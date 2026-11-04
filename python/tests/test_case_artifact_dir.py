from __future__ import annotations

import json
import keyword
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

import pytest

from optagent_core.contracts import canonical_json_bytes, strict_json_loads
from optagent_core.data import load_data_bundle
from optagent_core.models import build_native_model
from optagent_core.sources import case_artifact_dir, create_case_from_capture
from optagent_core.store import CaseStore


_SOLVE_OPTIONS = {
    "execution_budget_seconds": 60,
    "time_limit_seconds": 30,
    "relative_gap": 0.0,
    "absolute_gap": 0.0,
    "threads": 1,
    "random_seed": 0,
    "feasibility_tolerance": 1e-7,
    "integrality_tolerance": 1e-7,
    "write_log": False,
    "export_model": False,
}


def _create_case(core, *, title: str, text: bytes, seq: int):
    capture = core["capture"](text, seq=seq)
    reply = core["call"](
        "optagent.case",
        {
            "op": "create",
            "capture_id": capture["capture_id"],
            "title": title,
            "task_kind": "optimize",
            "world_relation": "single",
            "authorization_note": "Synthetic test fixture.",
        },
        captures=[capture],
    )
    assert reply["ok"], reply
    return reply["case_id"], reply["result"]["state"]


def test_artifact_dir_stays_stable_and_frozen_unicode_helper_is_loaded(core):
    title = "ＰＤＰ Dispatch / 演习"
    case_id, created = _create_case(
        core,
        title=title,
        text=b"Minimize the model objective under the declared bounds.",
        seq=1,
    )
    artifact_dir = created["artifact_dir"]
    component = PurePosixPath(artifact_dir).name
    slug = component
    assert artifact_dir.startswith("cases/")
    assert slug == "pdp_dispatch_演习"
    assert component.isidentifier()
    assert len(slug) <= 32
    assert (core["workspace"] / artifact_dir).is_dir()
    assert list((core["workspace"] / artifact_dir).iterdir()) == []

    store = core["store"]()
    state = store.read_case(case_id)
    renamed = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "kind": "display",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "title": "Renamed after creation",
        },
    )
    assert renamed["ok"], renamed
    assert renamed["result"]["state"]["artifact_dir"] == artifact_dir

    package_dir = "模块_构建"
    model_path = f"{artifact_dir}/{package_dir}/optimization_model.py"
    helper_path = f"{artifact_dir}/{package_dir}/辅助.py"
    (core["workspace"] / model_path).parent.mkdir(parents=True)
    (core["workspace"] / model_path).write_text(
        "from .辅助 import 下界\n"
        "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    model.x = Var(bounds=(下界(), 4))\n"
        "    model.cost = Objective(expr=model.x, sense=minimize)\n"
        "    return model\n",
        encoding="utf-8",
    )
    (core["workspace"] / helper_path).write_text(
        "def 下界():\n    return 2\n",
        encoding="utf-8",
    )
    state = store.read_case(case_id)
    candidate_reply = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "kind": "candidate",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "model_path": model_path,
            "local_module_paths": [helper_path],
            "data_id": None,
            "backend_id": "highs",
            "objective_plan": {
                "mode": "scalar",
                "objective_component": "cost",
                "sense": "min",
                "unit": "unitless",
                "preference_note": "Model loading only; no solver is called.",
            },
            "business_checker_path": None,
            "input_closure": "declared",
            "closure_note": "Both Unicode modules are declared in the local closure.",
        },
    )
    assert candidate_reply["ok"], candidate_reply
    candidate_id = candidate_reply["result"]["candidate"]["id"]

    state = store.read_case(case_id)
    prepared_reply = core["call"](
        "optagent.run",
        {
            "op": "prepare",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "candidate_id": candidate_id,
            "solve_options": dict(_SOLVE_OPTIONS),
        },
    )
    assert prepared_reply["ok"], prepared_reply
    prepared_id = prepared_reply["result"]["prepared"]["id"]
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json")
    manifest = strict_json_loads(store.read_ref(prepared["input_manifest"]))
    frozen_candidate = manifest["candidate"]
    bundle = load_data_bundle(
        store,
        None,
        candidate_binding_digest=manifest["binding_digest"],
    )
    model, consumed = build_native_model(
        store,
        bundle,
        frozen_candidate["model"],
        frozen_candidate["local_modules"],
    )
    assert model.x.lb == 2
    assert model.x.ub == 4
    assert {item["path"] for item in consumed} == {
        frozen_candidate["model"]["bytes"]["path"],
        frozen_candidate["local_modules"][0]["bytes"]["path"],
    }

    second_id, second_summary = _create_case(
        core,
        title=title,
        text=b"A second, independent case with the same display title.",
        seq=2,
    )
    assert second_id != case_id
    assert second_summary["artifact_dir"] == f"{artifact_dir}_2"
    assert second_summary["artifact_dir"] != artifact_dir
    state = store.read_case(case_id)
    listed = core["call"]("optagent.case", {"op": "list", "limit": 50})
    assert listed["ok"], listed
    by_id = {entry["case_id"]: entry for entry in listed["result"]["cases"]}
    assert by_id[case_id]["artifact_dir"] == artifact_dir
    assert by_id[second_id]["artifact_dir"] == second_summary["artifact_dir"]

    candidate_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json")
    source_id = state["source_ids"][0]
    source_record = store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json")
    claim = {
        "kind": "formulation_fidelity",
        "scope": "The registered candidate is bound to the active source.",
        "conditions": [],
        "evidence_refs": [candidate_ref, source_record["bytes"]],
        "support": "supported",
    }
    begun = core["call"](
        "optagent.review",
        {
            "op": "begin",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "candidate_id": candidate_id,
            "proof_ids": [],
            "run_ids": [],
        },
        role="opt-verifier",
    )
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    noted = core["call"](
        "optagent.review",
        {
            "op": "note",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": begun["result"]["review"]["review_revision"],
            "body": "The source is the complete scope for this synthetic fixture.",
            "source_locations": [{"source_id": source_id, "locator": "source-level"}],
        },
        role="opt-verifier",
    )
    assert noted["ok"], noted
    public_claim = {key: value for key, value in claim.items() if key != "evidence_refs"}
    public_claim["evidence_paths"] = [ref["path"] for ref in claim["evidence_refs"]]
    answer_path = "reports/approved-answer.md"
    answer_file = core["workspace"] / answer_path
    answer_file.parent.mkdir(parents=True, exist_ok=True)
    answer_file.write_text("The complete reviewed formulation answer.\n", encoding="utf-8")
    closed = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": noted["result"]["review"]["review_revision"],
            "record_kind": "outcome",
            "decision": "ready",
            "claims": [public_claim],
            "feedback": [],
            "remaining": [],
            "report_path": answer_path,
        },
        role="opt-verifier",
    )
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "mixed",
            "candidate_id": candidate_id,
            "review_id": review_id,
            "proof_ids": [],
            "run_ids": [],
        },
    )
    assert selected["ok"], selected
    reviewed = store.read_family_record(case_id, "reviews", review_id)[0]
    delivery = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{selected['result']['delivery']['id']}/delivery.json"
    )
    assert delivery["claims"] == reviewed["conclusion"]["claims"]
    assert delivery["reports"] == [reviewed["answer_ref"]]
    explicit_path = "用户指定/交付摘要.md"
    state = store.read_case(case_id)
    built = core["call"](
        "optagent.deliver",
        {
            "op": "build",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": selected["result"]["delivery"]["id"],
            "target_path": explicit_path,
        },
    )
    assert built["ok"], built
    assert built["result"]["report_ref"]["path"] == explicit_path
    assert (core["workspace"] / explicit_path).is_file()


def test_artifact_dir_legacy_fallback_is_read_only(core):
    case_id, _ = _create_case(
        core,
        title="Legacy case",
        text=b"The legacy case state has no artifact suggestion.",
        seq=1,
    )
    state_path = core["workspace"] / ".reaslab/optagent" / "cases" / case_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("artifact_dir")
    state_path.write_bytes(canonical_json_bytes(state))
    before = state_path.read_bytes()
    expected = f"cases/case_{case_id.replace('-', '')}"

    fetched = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert fetched["ok"], fetched
    assert fetched["result"]["state"]["artifact_dir"] == expected
    listed = core["call"]("optagent.case", {"op": "list", "limit": 50})
    assert listed["ok"], listed
    listed_case = next(item for item in listed["result"]["cases"] if item["case_id"] == case_id)
    assert listed_case["artifact_dir"] == expected
    assert state_path.read_bytes() == before


def test_artifact_dir_slug_is_import_safe_bounded_and_windows_safe():
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for title in ("class", "123 城市 / PDP", "🧭---", "A" * 200, "Ⅳ 物流", *sorted(reserved)):
        artifact_dir = case_artifact_dir(title)
        component = PurePosixPath(artifact_dir).name
        assert component.isidentifier()
        assert not keyword.iskeyword(component)
        assert len(component.encode("utf-8")) <= 128
        assert component.casefold() not in reserved
        assert PurePosixPath(artifact_dir).parts[0] == "cases"
    normalized = unicodedata.normalize("NFKC", "１２３ 物流")
    assert "123" in normalized
    assert case_artifact_dir("ABC") == case_artifact_dir("abc")


def test_same_title_cases_reserve_distinct_directories_under_concurrent_create(core):
    titles = ["Shared Plan", "shared plan", "SHARED PLAN", "Shared Plan"]
    with ThreadPoolExecutor(max_workers=len(titles)) as pool:
        created = list(pool.map(
            lambda item: _create_case(
                core,
                title=item[1],
                text=f"Independent case {item[0]}".encode(),
                seq=item[0] + 1,
            ),
            enumerate(titles),
        ))

    paths = [state["artifact_dir"] for _, state in created]
    assert len(set(paths)) == len(titles)
    assert set(paths) == {"cases/shared_plan", "cases/shared_plan_2", "cases/shared_plan_3", "cases/shared_plan_4"}
    assert all((core["workspace"] / path).is_dir() for path in paths)


def test_artifact_dir_skips_existing_user_paths_and_persisted_reservations(core, tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    occupied = cases_dir / "BUSY_TITLE"
    occupied.mkdir()
    marker = occupied / "keep.txt"
    marker.write_text("user data", encoding="utf-8")
    (cases_dir / "file_title").write_text("also user data", encoding="utf-8")

    _, busy = _create_case(core, title="Busy Title", text=b"A separate case.", seq=1)
    _, file = _create_case(core, title="File Title", text=b"Another separate case.", seq=2)
    assert busy["artifact_dir"] == "cases/busy_title_2"
    assert file["artifact_dir"] == "cases/file_title_2"
    assert marker.read_text(encoding="utf-8") == "user data"
    assert (cases_dir / "file_title").read_text(encoding="utf-8") == "also user data"

    _, first = _create_case(core, title="Reserved Later", text=b"Stored path reservation.", seq=3)
    reserved_path = tmp_path / first["artifact_dir"]
    reserved_path.rmdir()
    _, second = _create_case(core, title="Reserved Later", text=b"A new case with same title.", seq=4)
    assert second["artifact_dir"] == f"{first['artifact_dir']}_2"
    assert not reserved_path.exists()


def test_artifact_dir_treats_symlink_as_occupied_without_following_it(core, tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    link = cases_dir / "linked_title"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    _, created = _create_case(core, title="Linked Title", text=b"A synthetic case.", seq=1)
    assert created["artifact_dir"] == "cases/linked_title_2"
    assert link.is_symlink()
    assert list(target.iterdir()) == []


def test_unreadable_unrelated_case_state_does_not_block_case_creation(core, tmp_path):
    case_id, _ = _create_case(core, title="Existing Case", text=b"Existing fixture.", seq=1)
    state_path = tmp_path / ".reaslab/optagent" / "cases" / case_id / "state.json"
    state_path.write_text("{", encoding="utf-8")

    _, created = _create_case(core, title="Independent Case", text=b"New fixture.", seq=2)
    assert created["artifact_dir"] == "cases/independent_case"


def test_case_artifact_reservation_cleanup_only_removes_its_empty_directory(core, monkeypatch, tmp_path):
    store = core["store"]()
    transfer = core["capture"](b"A case whose initial state write fails.", seq=1)
    stamp = core["context"]()["stamp"]
    original_write = CaseStore._atomic_write

    def fail_state_write(self, path, raw, *, replace):
        if path.name == "state.json" and path.parent.parent == self.root / "cases":
            raise OSError("synthetic state write failure")
        return original_write(self, path, raw, replace=replace)

    monkeypatch.setattr(CaseStore, "_atomic_write", fail_state_write)
    with pytest.raises(OSError, match="synthetic state write failure"):
        create_case_from_capture(
            store,
            title="Empty Reservation",
            task_kind="optimize",
            world_relation="single",
            authorization_note="Synthetic test.",
            capture_transfer=transfer,
            stamp=stamp,
        )
    assert not (tmp_path / "cases" / "empty_reservation").exists()


def test_case_artifact_reservation_cleanup_preserves_a_file_added_before_failure(core, monkeypatch, tmp_path):
    store = core["store"]()
    transfer = core["capture"](b"A case whose state write fails after an outside file appears.", seq=1)
    stamp = core["context"]()["stamp"]
    original_write = CaseStore._atomic_write

    def fail_state_write(self, path, raw, *, replace):
        if path.name == "state.json" and path.parent.parent == self.root / "cases":
            artifact_dir = tmp_path / "cases" / "kept_nonempty"
            (artifact_dir / "keep.txt").write_text("preserve", encoding="utf-8")
            raise OSError("synthetic state write failure")
        return original_write(self, path, raw, replace=replace)

    monkeypatch.setattr(CaseStore, "_atomic_write", fail_state_write)
    with pytest.raises(OSError, match="synthetic state write failure"):
        create_case_from_capture(
            store,
            title="Kept Nonempty",
            task_kind="optimize",
            world_relation="single",
            authorization_note="Synthetic test.",
            capture_transfer=transfer,
            stamp=stamp,
        )
    assert (tmp_path / "cases" / "kept_nonempty" / "keep.txt").read_text(encoding="utf-8") == "preserve"
