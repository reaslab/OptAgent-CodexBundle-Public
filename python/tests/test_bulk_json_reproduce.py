from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from optagent_core.contracts import canonical_json_bytes, ref_from_bytes, sha256_bytes
from optagent_core.data import normalize_data
from family_fixtures import current_portable_review_fixture


_DEFAULT_JSON_LIMIT = 4 * 1024 * 1024


@pytest.fixture
def short_root(tmp_path):
    root = tmp_path.parents[1] / f"r{uuid.uuid4().hex[:7]}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def test_stdlib_bootstrap_accepts_large_refmapped_data_manifest_trace_and_values(short_root):
    case_id = str(uuid.uuid4())
    data_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    object_id = "items"

    class MemoryWriter:
        def __init__(self):
            self.blobs = {}

        @staticmethod
        def key(ref):
            return ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"]

        def read_ref(self, ref):
            return self.blobs[self.key(ref)]

        def write_immutable(self, path, raw, media_type):
            ref = ref_from_bytes(path, raw, media_type)
            self.blobs[self.key(ref)] = raw
            return ref

    writer = MemoryWriter()
    source_raw = b"id,value\nA,7\n"
    source_bytes_ref = writer.write_immutable(
        f".reaslab/optagent/source-bytes/{sha256_bytes(source_raw)}", source_raw, "application/octet-stream"
    )
    source_record = {
        "source_id": source_id,
        "purpose": "data",
        "kind": "file",
        "bytes": source_bytes_ref,
        "original_sha256": source_bytes_ref["sha256"],
        "original_size": len(source_raw),
        "origin": {},
    }
    plan = {
        "encoding": "utf-8",
        "blocks": [{
            "object_id": object_id,
            "source_id": source_id,
            "format": "csv",
            "layout": {"delimiter": ",", "header": "first_row", "skip_lines": 0},
            "kind": "table",
            "key_fields": ["id"],
            "columns": [
                {"name": "id", "position": None, "input_name": "id", "type": "string", "unit": None, "nullable": False},
                {"name": "value", "position": None, "input_name": "value", "type": "integer", "unit": None, "nullable": False},
            ],
            "duplicate_policy": "error",
            "missing_policy": "error",
        }],
        "conversions": [],
    }
    normalized = normalize_data(
        writer,
        data_id=data_id,
        source_records={source_id: source_record},
        source_bytes={source_id: source_raw},
        plan=plan,
        target_prefix=f".reaslab/optagent/cases/{case_id}/data/{data_id}",
    )
    manifest = normalized["manifest"]
    original_values_ref = manifest["objects"][0]["values"]
    original_trace_ref = manifest["trace"]
    values_raw = b" " * (_DEFAULT_JSON_LIMIT + 9) + writer.read_ref(original_values_ref)
    trace_raw = b" " * (_DEFAULT_JSON_LIMIT + 11) + writer.read_ref(original_trace_ref)
    values_ref = ref_from_bytes(original_values_ref["path"], values_raw, "application/json")
    trace_ref = ref_from_bytes(original_trace_ref["path"], trace_raw, "application/json")
    manifest["objects"][0]["values"] = values_ref
    manifest["trace"] = trace_ref
    digest_material = {key: value for key, value in manifest.items() if key != "digest"}
    digest = sha256_bytes(canonical_json_bytes(digest_material))
    manifest["digest"] = digest
    manifest_raw = b" " * (_DEFAULT_JSON_LIMIT + 13) + canonical_json_bytes(manifest)
    manifest_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json", manifest_raw, "application/json"
    )
    refs = [
        (manifest_ref, f"data/{data_id}/manifest.json", manifest_raw),
        (trace_ref, f"data/{data_id}/trace.json", trace_raw),
        (values_ref, f"data/{data_id}/objects/{hashlib.sha256(object_id.encode('utf-8')).hexdigest()}.json", values_raw),
    ]
    source_record_raw = canonical_json_bytes(source_record)
    source_record_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json",
        source_record_raw,
        "application/json",
    )
    refs.extend([
        (source_record_ref, f"data/{data_id}/sources/{source_id}/record.json", source_record_raw),
        (source_bytes_ref, f"data/{data_id}/sources/{source_id}/bytes.bin", source_raw),
    ])
    package_root = short_root / "package"
    package_root.mkdir()
    for _, package_path, raw in refs:
        destination = package_root.joinpath(*package_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    delivery_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    fixture = current_portable_review_fixture(
        writer,
        case_id=case_id,
        review_id=review_id,
        delivery_id=delivery_id,
        kind="proof",
        claim={
            "kind": "diagnostic",
            "scope": "This synthetic fixture carries normalized data but makes no solver conclusion.",
            "support": "unresolved",
            "conditions": [],
            "evidence_refs": [],
        },
    )
    delivery = fixture["selection"]
    selection_ref = fixture["selection_ref"]
    review_raw = next(raw for _ref, path, raw in fixture["portable_files"] if path == f"evidence/reviews/{review_id}.json")
    review = json.loads(review_raw)
    assert review["note"]["source_locations"] == []
    assert review["candidate_opened"] is True
    assert review["binding_evidence_refs"] == {"candidate": None, "proofs": {}, "runs": {}}
    assert review["binding"]["run_ids"] == []
    assert review["note"]["body"] in fixture["consumed_refs"]
    selection_path = package_root / "evidence" / "selection.json"
    selection_path.parent.mkdir(parents=True)
    for fixture_ref, package_path, raw in fixture["portable_files"]:
        destination = package_root.joinpath(*package_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        refs.append((fixture_ref, package_path, raw))
    package_manifest = {
        "schema_version": 1,
        "case_id": case_id,
        "delivery_id": delivery_id,
        "selection_ref": selection_ref,
        "authority_records": delivery["authority_records"],
        "consumed_refs": fixture["consumed_refs"],
        "objects": [],
        "claims": delivery["claims"],
        "reviews": [fixture["projection"]],
        "claim_attachments": [],
        "data_bundles": [
            {"data_id": data_id, "digest": digest, "manifest": manifest_ref, "loader_version": "optagent-data-v1"}
        ],
        "runtime_bundles": [],
        "reproductions": [],
        "ref_map": [
            {"ref": ref, "package_path": package_path}
            for ref, package_path, _ in sorted(refs, key=lambda item: item[0]["path"])
        ],
        "entry": "README.md",
    }
    (package_root / "manifest.json").write_bytes(canonical_json_bytes(package_manifest))
    (package_root / "README.md").write_text("Data evidence fixture.\n", encoding="utf-8")
    reproduce_dir = package_root / "reproduce"
    reproduce_dir.mkdir()
    bootstrap = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    script = reproduce_dir / "run.py"
    script.write_bytes(bootstrap.read_bytes())

    output = short_root / "unused-output"
    completed = subprocess.run(
        [sys.executable, "-I", str(script), "--output-dir", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "no supported standalone native reproduction entry" in completed.stderr
    assert "fixed size limit" not in completed.stderr
    assert not output.exists()

    # The bootstrap accepted the exact refs, not merely their declared lengths.
    changed = package_root.joinpath(*refs[2][1].split("/"))
    changed.write_bytes(b"changed")
    rejected = subprocess.run(
        [sys.executable, "-I", str(script), "--output-dir", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "packaged bytes do not match their original Ref" in rejected.stderr
    assert not output.exists()
