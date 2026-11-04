from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes, ref_from_bytes
from optagent_core.data import _validate_layout, load_data_bundle, load_local_data_bundle, normalize_data


class _MemoryWriter:
    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str, int, str], bytes] = {}
        self.source_records: dict[str, dict[str, Any]] = {}
        self.immutable_writes: list[str] = []

    @staticmethod
    def _key(ref: dict[str, Any]) -> tuple[str, str, int, str]:
        return ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"]

    def put_source(self, raw: bytes) -> dict[str, Any]:
        ref = ref_from_bytes(f".reaslab/optagent/source-bytes/{hashlib.sha256(raw).hexdigest()}", raw, "application/octet-stream")
        self.blobs[self._key(ref)] = raw
        return ref

    def read_ref(self, ref: dict[str, Any]) -> bytes:
        return self.blobs[self._key(ref)]

    def write_immutable(self, relative_path: str, raw: bytes, media_type: str) -> dict[str, Any]:
        self.immutable_writes.append(relative_path)
        ref = ref_from_bytes(relative_path, raw, media_type)
        self.blobs[self._key(ref)] = raw
        return ref


def _column(
    name: str,
    position: int | None,
    kind: str = "string",
    *,
    nullable: bool = False,
    input_name: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "position": position,
        "input_name": input_name,
        "type": kind,
        "unit": None,
        "nullable": nullable,
    }


def _whitespace_block(
    *,
    layout: dict[str, Any] | None = None,
    columns: list[dict[str, Any]] | None = None,
    kind: str = "table",
    key_fields: list[str] | None = None,
    missing_policy: str = "error",
    object_id: str = "items",
) -> dict[str, Any]:
    declared_columns = columns if columns is not None else [_column("value", 0)]
    declared_keys = key_fields if key_fields is not None else ([declared_columns[0]["name"]] if kind == "table" else [])
    return {
        "object_id": object_id,
        "source_id": "source-1",
        "format": "whitespace_table",
        "layout": layout
        or {
            "skip_lines": 0,
            "header": "none",
            "take_rows": None,
            "skip_blank_lines": True,
            "field_count": 1,
        },
        "kind": kind,
        "key_fields": declared_keys,
        "columns": declared_columns,
        "duplicate_policy": "error",
        "missing_policy": missing_policy,
    }


def _csv_block(layout: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "object_id": "csv_rows",
        "source_id": "source-1",
        "format": "csv",
        "layout": layout or {"delimiter": ",", "header": "first_row", "skip_lines": 0},
        "kind": "table",
        "key_fields": ["site"],
        "columns": [
            _column("site", None, input_name="site"),
            _column("note", None, input_name="note"),
        ],
        "duplicate_policy": "error",
        "missing_policy": "error",
    }


def _json_block(pointer: str, *, object_id: str = "native_json") -> dict[str, Any]:
    return {
        "object_id": object_id,
        "source_id": "source-1",
        "format": "json",
        "layout": {"pointer": pointer},
        "kind": "json",
        "key_fields": [],
        "columns": [],
        "duplicate_policy": "error",
        "missing_policy": "error",
    }


def _normalize(raw: bytes, block: dict[str, Any]) -> tuple[_MemoryWriter, dict[str, Any]]:
    writer = _MemoryWriter()
    source_ref = writer.put_source(raw)
    source = {
        "source_id": "source-1",
        "purpose": "data",
        "kind": "file",
        "bytes": source_ref,
        "original_sha256": source_ref["sha256"],
        "original_size": len(raw),
        "origin": {},
    }
    writer.source_records = {"source-1": source}
    result = normalize_data(
        writer,
        data_id="test-data",
        source_records=writer.source_records,
        source_bytes={"source-1": raw},
        plan={"encoding": "utf-8", "blocks": [block], "conversions": []},
        target_prefix=".normalized/test-data",
    )
    return writer, result


def _expect_invalid(raw: bytes, block: dict[str, Any], pointer: str | None = None) -> ContractError:
    with pytest.raises(ContractError) as caught:
        _normalize(raw, block)
    assert caught.value.code == "invalid_argument"
    if pointer is not None:
        assert caught.value.pointer == pointer
    return caught.value


def _scalar_block(position: int = 0, *, kind: str = "integer", take_rows: int | None = 1) -> dict[str, Any]:
    return _whitespace_block(
        layout={
            "skip_lines": 0,
            "header": "none",
            "take_rows": take_rows,
            "skip_blank_lines": True,
            "field_count": 1,
        },
        columns=[_column("value", position, kind)],
        kind="scalar",
    )


def test_real_c101_segments_reach_capture_normalize_inspect_and_shared_loader(core):
    fixture_value = os.environ.get("OPTAGENT_TEST_C101")
    if fixture_value is None:
        pytest.skip("set OPTAGENT_TEST_C101 to the externally supplied original C101 fixture")
    fixture_path = Path(fixture_value)
    assert fixture_path.is_absolute(), "OPTAGENT_TEST_C101 must name an absolute external fixture path"
    raw = fixture_path.read_bytes()
    assert len(raw) == 7504
    assert hashlib.sha256(raw).hexdigest() == "f15fcbad39f3ec3c84a043339ee1722519f99242f34ca2b65cdb224064f486fa"

    case_id, _, _ = core["create_case"]()
    inputs = core["workspace"] / "inputs"
    inputs.mkdir()
    (inputs / "c101.txt").write_bytes(raw)
    state = core["store"]().read_case(case_id)
    captured = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "inputs/c101.txt",
            "purpose": "data",
            "locations": ["original C101 data file"],
        },
    )
    source_id = captured["result"]["source"]["id"]

    def block(object_id: str, *, skip: int, take: int | None, field_count: int, kind: str, columns: list[dict[str, Any]], key_fields: list[str] | None = None):
        return {
            "object_id": object_id,
            "source_id": source_id,
            "format": "whitespace_table",
            "layout": {
                "skip_lines": skip,
                "header": "none",
                "take_rows": take,
                "skip_blank_lines": True,
                "field_count": field_count,
            },
            "kind": kind,
            "key_fields": key_fields or [],
            "columns": columns,
            "duplicate_policy": "error",
            "missing_policy": "error",
        }

    node_names = ["node_id", "x", "y", "demand", "ready_time", "due_date", "service_time"]
    plan = {
        "encoding": "utf-8",
        "blocks": [
            block("c101_range", skip=1, take=1, field_count=1, kind="scalar", columns=[_column("range", 0, "integer")]),
            block("c101_launch", skip=4, take=1, field_count=2, kind="scalar", columns=[_column("launch", 0, "integer")]),
            block("c101_recover", skip=4, take=1, field_count=2, kind="scalar", columns=[_column("recover", 1, "integer")]),
            block(
                "c101_nodes",
                skip=8,
                take=None,
                field_count=7,
                kind="table",
                key_fields=["node_id"],
                columns=[_column(name, index, "integer") for index, name in enumerate(node_names)],
            ),
        ],
        "conversions": [],
    }
    current = core["store"]().read_case(case_id)
    normalized = core["call"](
        "optagent.data",
        {
            "op": "normalize",
            "case_id": case_id,
            "expected_state_revision": current["state_revision"],
            "source_ids": [source_id],
            "plan": plan,
        },
    )
    data = normalized["result"]["data_manifest"]
    inspected = core["call"](
        "optagent.data",
        {"op": "inspect", "case_id": case_id, "data_id": data["data_id"], "sample_rows": 100},
    )
    samples = {sample["object_id"]: sample for sample in inspected["result"]["samples"]}
    assert samples["c101_range"]["rows"] == [100]
    assert samples["c101_launch"]["rows"] == [1]
    assert samples["c101_recover"]["rows"] == [1]
    assert samples["c101_nodes"]["total"] == 101
    assert samples["c101_nodes"]["truncated"] is True

    store = core["store"]()
    source_record = store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json")
    source_ref = source_record["bytes"]
    assert source_ref["byte_len"] == len(raw)
    assert source_ref["sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.read_ref(source_ref) == raw
    manifest = json.loads(store.read_ref(data["manifest_ref"]))
    trace = json.loads(store.read_ref(manifest["trace"]))
    assert trace["plan"] == plan
    assert trace["source_refs"] == [source_ref]

    bundle = load_data_bundle(store, data["manifest_ref"], source_records={source_id: source_record})
    assert bundle.scalar("c101_range") == 100
    assert bundle.scalar("c101_launch") == 1
    assert bundle.scalar("c101_recover") == 1
    nodes = bundle.object("c101_nodes")
    lines = raw.decode("utf-8", errors="strict").splitlines()
    expected_nodes = [
        {name: int(token) for name, token in zip(node_names, line.split(), strict=True)}
        for line in lines[8:]
        if line.strip()
    ]
    assert list(nodes.rows) == expected_nodes
    assert [loc["locator"] for loc in bundle.object("c101_range").source_locations] == ["line 2"]
    assert [loc["locator"] for loc in bundle.object("c101_launch").source_locations] == ["line 5"]
    assert [loc["locator"] for loc in bundle.object("c101_recover").source_locations] == ["line 5"]
    assert [loc["locator"] for loc in nodes.source_locations] == [f"line {line}" for line in range(9, 110)]


def test_native_json_pointer_preserves_one_complete_node_and_isolates_getters():
    raw = b'{"by/key~": {"matrix": [[9007199254740993, null], [0, 0]], "empty": {}, "items": ["x", "x"]}}'
    pointer = "/by~1key~0"
    writer, result = _normalize(raw, _json_block(pointer))
    manifest = result["manifest"]
    spec = manifest["objects"][0]
    expected = {
        "matrix": [[9007199254740993, None], [0, 0]],
        "empty": {},
        "items": ["x", "x"],
    }

    assert spec["kind"] == "json"
    assert spec["key_fields"] == []
    assert spec["columns"] == []
    assert json.loads(writer.read_ref(spec["values"])) == [expected]
    assert spec["source_locations"] == [{"source_id": "source-1", "locator": f"JSON pointer {pointer}"}]

    bundle = load_data_bundle(writer, manifest, source_records=writer.source_records)
    first = bundle.json("native_json")
    assert first == expected
    first["matrix"][0][0] = 3
    first["items"].append("changed")
    assert bundle.json("native_json") == expected
    assert bundle.object("native_json").rows == ()
    for getter in (bundle.rows, bundle.table, bundle.scalar, bundle.set, bundle.relation):
        with pytest.raises(ContractError) as caught:
            getter("native_json")
        assert caught.value.code == "invalid_argument"

    local = load_local_data_bundle({
        "schema_version": 1,
        "instance_parameter_object_id": None,
        "objects": [{
            "object_id": "native_json",
            "kind": "json",
            "key_fields": [],
            "columns": [],
            "values": [expected],
        }],
    })
    assert local.json("native_json") == expected
    with pytest.raises(ContractError) as caught:
        load_local_data_bundle({
            "schema_version": 1,
            "instance_parameter_object_id": "native_json",
            "objects": [{
                "object_id": "native_json",
                "kind": "json",
                "key_fields": [],
                "columns": [],
                "values": [expected],
            }],
        })
    assert caught.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "raw, pointer, expected",
    [
        (b"null", "", None),
        (b"23", "", 23),
        (b'"text"', "", "text"),
        (b"[]", "", []),
    ],
)
def test_native_json_accepts_top_level_scalars_null_and_empty_containers(raw, pointer, expected):
    writer, result = _normalize(raw, _json_block(pointer))
    bundle = load_data_bundle(writer, result["manifest"], source_records=writer.source_records)
    assert bundle.json("native_json") == expected
    assert len(bundle.object("native_json").source_locations) == 1


def test_native_json_rejects_missing_pointer_nonfinite_values_and_conversion():
    with pytest.raises(ContractError) as missing:
        _normalize(b'{"present": null}', _json_block("/absent"))
    assert missing.value.code == "not_found"

    with pytest.raises(ContractError) as non_ascii_index:
        _normalize(b"[7]", _json_block("/\u0660"))
    assert non_ascii_index.value.code == "not_found"

    with pytest.raises(ContractError) as nonfinite:
        _normalize(b'{"present": NaN}', _json_block("/present"))
    assert nonfinite.value.code == "invalid_argument"

    block = _json_block("/present")
    conversion = {
        "object_id": "native_json", "column": "anything", "kind": "scale",
        "factor": 2, "offset": None, "format": None, "timezone": None, "basis": [],
    }
    writer = _MemoryWriter()
    raw = b'{"present": 2}'
    source_ref = writer.put_source(raw)
    source = {
        "source_id": "source-1", "purpose": "data", "kind": "file", "bytes": source_ref,
        "original_sha256": source_ref["sha256"], "original_size": len(raw), "origin": {},
    }
    with pytest.raises(ContractError) as converted:
        normalize_data(
            writer,
            data_id="test-data",
            source_records={"source-1": source},
            source_bytes={"source-1": raw},
            plan={"encoding": "utf-8", "blocks": [block], "conversions": [conversion]},
            target_prefix=".normalized/test-data",
        )
    assert converted.value.code == "invalid_argument"


def test_data_inspect_bounds_json_prefix_by_shared_nodes_depth_and_characters(core):
    case_id, _, _ = core["create_case"]()
    input_dir = core["workspace"] / "inputs"
    input_dir.mkdir()
    raw = json.dumps({"tiny": {"a": [1, 1]}, "deep": [[[[[{"leaf": 1}]]]]], "long": "z" * 6000}, separators=(",", ":")).encode()
    (input_dir / "nested.json").write_bytes(raw)
    state = core["store"]().read_case(case_id)
    captured = core["call"]("optagent.data", {
        "op": "capture", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "path": "inputs/nested.json", "purpose": "data", "locations": ["complete JSON input"],
    })
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    state = core["store"]().read_case(case_id)
    blocks = [_json_block("/tiny", object_id="tiny"), _json_block("/deep", object_id="deep"), _json_block("/long", object_id="long")]
    for block in blocks:
        block["source_id"] = source_id
    normalized = core["call"]("optagent.data", {
        "op": "normalize", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "source_ids": [source_id],
        "plan": {"encoding": "utf-8", "blocks": blocks, "conversions": []},
    })
    assert normalized["ok"], normalized
    data_id = normalized["result"]["data_manifest"]["data_id"]

    inspected = core["call"]("optagent.data", {
        "op": "inspect", "case_id": case_id, "data_id": data_id, "sample_rows": 100,
    })
    assert inspected["ok"], inspected
    samples = {sample["object_id"]: sample for sample in inspected["result"]["samples"]}
    assert all(sample["kind"] == "json" and sample["total"] == 1 for sample in samples.values())
    assert samples["tiny"]["truncated"] is False
    assert samples["deep"]["truncated"] is True
    assert samples["long"]["truncated"] is True
    assert sum(len(json.dumps(sample["rows"][0], ensure_ascii=False, separators=(",", ":"))) for sample in samples.values() if sample["rows"]) <= 4096
    assert samples["long"]["rows"][0].startswith("z")
    assert len(samples["long"]["rows"][0]) < 6000

    def depth(value):
        if isinstance(value, dict):
            return 0 if not value else 1 + max(depth(child) for child in value.values())
        if isinstance(value, list):
            return 0 if not value else 1 + max(depth(child) for child in value)
        return 0

    assert depth(samples["deep"]["rows"][0]) <= 4

    small = core["call"]("optagent.data", {
        "op": "inspect", "case_id": case_id, "data_id": data_id, "sample_rows": 4,
    })
    small_samples = {sample["object_id"]: sample for sample in small["result"]["samples"]}
    assert small_samples["tiny"]["truncated"] is False
    assert small_samples["deep"]["rows"] == []
    assert small_samples["long"]["rows"] == []
    assert small_samples["long"]["truncated"] is True

    none = core["call"]("optagent.data", {
        "op": "inspect", "case_id": case_id, "data_id": data_id, "sample_rows": 0,
    })
    assert all(sample["rows"] == [] and sample["truncated"] is True for sample in none["result"]["samples"])


def test_bounded_whitespace_stops_before_tail_checks_but_keeps_selected_errors():
    bounded = _whitespace_block()
    bounded["layout"]["take_rows"] = 1
    writer, result = _normalize(b"1\nBAD WIDTH\n", bounded)
    assert result["manifest"]["objects"][0]["source_locations"] == [{"source_id": "source-1", "locator": "line 1"}]
    with pytest.raises(ContractError, match="unexpected field count"):
        _normalize(b"1\nBAD WIDTH\n", _whitespace_block())

    _expect_invalid(b"bad\n", _scalar_block(), "line 1/value")
    wrong_width = _whitespace_block()
    wrong_width["layout"]["take_rows"] = 1
    _expect_invalid(b"1 2\nBAD TAIL\n", wrong_width, "/line/1")

    duplicate = _whitespace_block(
        layout={"skip_lines": 0, "header": "none", "take_rows": 2, "skip_blank_lines": True, "field_count": 1},
        columns=[_column("key", 0)],
    )
    _expect_invalid(b"same\nsame\n", duplicate, "/line/line 2")

    non_finite = _scalar_block(kind="number")
    _expect_invalid(b"NaN\nBAD TAIL\n", non_finite, "line 1/value")
    assert writer.read_ref(result["manifest_ref"])


def test_header_blank_and_nullable_rows_follow_data_row_boundaries():
    header_plan = _whitespace_block(
        layout={"skip_lines": 0, "header": "first_row", "take_rows": 2, "skip_blank_lines": True, "field_count": 2},
        columns=[_column("name", None, input_name="name"), _column("amount", None, "integer", input_name="amount")],
        key_fields=["name"],
    )
    _, result = _normalize(b"name amount\nA 1\n\nB 2\nTAIL BAD WIDTH\n", header_plan)
    assert [item["locator"] for item in result["manifest"]["objects"][0]["source_locations"]] == ["line 2", "line 4"]

    zero_header = dict(header_plan)
    zero_header["layout"] = {**header_plan["layout"], "take_rows": 0}
    _, empty = _normalize(b"name amount\nBAD WIDTH\n", zero_header)
    assert empty["manifest"]["objects"][0]["source_locations"] == []
    for raw, pointer in [(b"", None), (b"a a\n", "/line/1"), (b"single\n", "/line/1")]:
        error = _expect_invalid(raw, zero_header)
        if pointer is not None:
            assert error.pointer == pointer
        else:
            assert "header is missing" in error.message

    allow_blank = _whitespace_block()
    allow_blank["layout"]["take_rows"] = 2
    _, blank_ok = _normalize(b"A\n\nB\n", allow_blank)
    assert [item["locator"] for item in blank_ok["manifest"]["objects"][0]["source_locations"]] == ["line 1", "line 3"]

    reject_blank = _whitespace_block(
        layout={"skip_lines": 0, "header": "none", "take_rows": 2, "skip_blank_lines": False, "field_count": 1}
    )
    _expect_invalid(b"A\n\nB\n", reject_blank, "/line/2")
    edge_blank = dict(reject_blank)
    edge_blank["layout"] = {**reject_blank["layout"], "take_rows": 1}
    _, edge_result = _normalize(b"A\n\nBAD WIDTH\n", edge_blank)
    assert len(edge_result["manifest"]["objects"][0]["source_locations"]) == 1

    nullable = _whitespace_block(
        layout={"skip_lines": 0, "header": "none", "take_rows": 1, "skip_blank_lines": True, "field_count": 1},
        columns=[_column("key", 0), _column("maybe", 1, "integer", nullable=True)],
        key_fields=["key"],
        missing_policy="allow_nullable",
    )
    nullable_writer, nullable_result = _normalize(b"A\nB C\n", nullable)
    values_ref = nullable_result["manifest"]["objects"][0]["values"]
    assert nullable_result["manifest"]["objects"][0]["source_locations"] == [
        {"source_id": "source-1", "locator": "line 1"}
    ]
    assert json.loads(nullable_writer.read_ref(values_ref)) == [{"key": "A", "maybe": None}]
    nullable_two = dict(nullable)
    nullable_two["layout"] = {**nullable["layout"], "take_rows": 2}
    _expect_invalid(b"A\nB C\n", nullable_two, "/line/2")


@pytest.mark.parametrize("bad_skip", [-1, True, 1.5, "1", None])
@pytest.mark.parametrize("fmt", ["csv", "whitespace_table"])
def test_skip_lines_is_a_non_negative_integer_for_csv_and_whitespace(fmt, bad_skip):
    layout = (
        {"delimiter": ",", "header": "first_row", "skip_lines": bad_skip}
        if fmt == "csv"
        else {"skip_lines": bad_skip, "header": "none", "take_rows": None, "skip_blank_lines": True, "field_count": 1}
    )
    with pytest.raises(ContractError) as caught:
        _validate_layout(fmt, layout, "/plan/blocks/0")
    assert caught.value.code == "invalid_argument"
    assert caught.value.pointer == "/plan/blocks/0/layout/skip_lines"


def test_eof_zero_and_scalar_exact_one_use_actual_rows():
    empty_table = _whitespace_block(
        layout={"skip_lines": 1, "header": "none", "take_rows": None, "skip_blank_lines": True, "field_count": 1}
    )
    _, equal = _normalize(b"only\n", empty_table)
    assert equal["manifest"]["objects"][0]["source_locations"] == []
    too_many_skips = dict(empty_table)
    too_many_skips["layout"] = {**empty_table["layout"], "skip_lines": 2}
    _expect_invalid(b"only\n", too_many_skips)

    zero = _whitespace_block(
        layout={"skip_lines": 0, "header": "none", "take_rows": 0, "skip_blank_lines": False, "field_count": 1}
    )
    _, zero_result = _normalize(b"\nBAD WIDTH\n", zero)
    assert zero_result["manifest"]["objects"][0]["source_locations"] == []
    zero_scalar = _scalar_block(take_rows=0)
    _expect_invalid(b"1\n", zero_scalar)

    roomy_scalar = _scalar_block(take_rows=3)
    writer, one_actual = _normalize(b"1\n", roomy_scalar)
    bundle = load_data_bundle(writer, one_actual["manifest_ref"], source_records=writer.source_records)
    assert bundle.scalar("items") == 1
    _expect_invalid(b"", roomy_scalar)
    two = dict(roomy_scalar)
    two["layout"] = {**roomy_scalar["layout"], "field_count": 1}
    _expect_invalid(b"1\n2\n", two)


def _single_value_block(fmt: str, kind: str, column_count: int) -> dict[str, Any]:
    columns = [_column(f"value_{index}", index, "integer") for index in range(column_count)]
    if fmt == "json":
        layout = {"pointer": ""}
    elif fmt == "csv":
        layout = {"delimiter": ",", "header": "none", "skip_lines": 0}
    else:
        layout = {
            "skip_lines": 0,
            "header": "none",
            "take_rows": None,
            "skip_blank_lines": True,
            "field_count": column_count,
        }
    return {
        "object_id": "value",
        "source_id": "source-1",
        "format": fmt,
        "layout": layout,
        "kind": kind,
        "key_fields": [],
        "columns": columns,
        "duplicate_policy": "error",
        "missing_policy": "error",
    }


@pytest.mark.parametrize("fmt", ["json", "csv", "whitespace_table"])
@pytest.mark.parametrize("kind", ["scalar", "set"])
def test_scalar_and_set_reject_multiple_columns_before_value_writes(fmt, kind):
    if fmt == "json":
        raw = b'[{"value_0":1,"value_1":2}]'
        valid_layout = {"pointer": ""}
        valid_columns = [
            _column("value_0", None, "integer", input_name="value_0"),
            _column("value_1", None, "integer", input_name="value_1"),
        ]
    elif fmt == "csv":
        raw = b"1,2\n"
        valid_layout = {"delimiter": ",", "header": "none", "skip_lines": 0}
        valid_columns = [_column("value_0", 0, "integer"), _column("value_1", 1, "integer")]
    else:
        raw = b"1 2\n"
        valid_layout = {
            "skip_lines": 0, "header": "none", "take_rows": None,
            "skip_blank_lines": True, "field_count": 2,
        }
        valid_columns = [_column("value_0", 0, "integer"), _column("value_1", 1, "integer")]
    valid_block = {
        "object_id": "valid-prefix",
        "source_id": "source-1",
        "format": fmt,
        "layout": valid_layout,
        "kind": "table",
        "key_fields": ["value_0"],
        "columns": valid_columns,
        "duplicate_policy": "error",
        "missing_policy": "error",
    }
    block = _single_value_block(fmt, kind, 2)
    writer = _MemoryWriter()
    source_ref = writer.put_source(raw)
    source_records = {
        "source-1": {
            "source_id": "source-1", "purpose": "data", "kind": "file",
            "bytes": source_ref, "original_sha256": source_ref["sha256"],
            "original_size": len(raw), "origin": {},
        }
    }
    writer.source_records = source_records
    with pytest.raises(ContractError) as caught:
        normalize_data(
            writer,
            data_id="multi-column",
            source_records=source_records,
            source_bytes={"source-1": raw},
            plan={"encoding": "utf-8", "blocks": [valid_block, block], "conversions": []},
            target_prefix=".normalized/multi-column",
        )
    assert caught.value.code == "invalid_argument"
    assert caught.value.pointer == "/plan/blocks/1/columns"
    assert writer.immutable_writes == []


@pytest.mark.parametrize("fmt", ["json", "csv", "whitespace_table"])
@pytest.mark.parametrize("kind", ["scalar", "set"])
def test_scalar_and_set_accept_one_column_for_each_import_format(fmt, kind):
    if fmt == "json":
        raw = b"1" if kind == "scalar" else b"[1,2]"
        expected = [1] if kind == "scalar" else [1, 2]
    elif fmt == "csv":
        raw = b"1\n" if kind == "scalar" else b"1\n2\n"
        expected = [1] if kind == "scalar" else [1, 2]
    else:
        raw = b"1\n" if kind == "scalar" else b"1\n2\n"
        expected = [1] if kind == "scalar" else [1, 2]
    block = _single_value_block(fmt, kind, 1)
    writer, normalized = _normalize(raw, block)
    values_ref = normalized["manifest"]["objects"][0]["values"]
    assert json.loads(writer.read_ref(values_ref)) == expected


@pytest.mark.parametrize("bad_take", [-1, True, 1.5, "1"])
def test_take_rows_rejects_non_integer_or_negative_values(bad_take):
    block = _whitespace_block()
    block["layout"]["take_rows"] = bad_take
    _expect_invalid(b"A\n", block)


def test_complete_source_refs_and_digest_change_with_unselected_bytes_and_utf8_stays_strict():
    block = _whitespace_block()
    block["layout"]["take_rows"] = 1
    first_writer, first = _normalize(b"1\nunused A\n", block)
    same_writer, same = _normalize(b"1\nunused A\n", block)
    changed_writer, changed = _normalize(b"1\nunused B\n", block)
    assert first["manifest"]["digest"] == same["manifest"]["digest"]
    assert first_writer.read_ref(first["manifest"]["objects"][0]["values"]) == same_writer.read_ref(same["manifest"]["objects"][0]["values"])

    first_trace = json.loads(first_writer.read_ref(first["manifest"]["trace"]))
    changed_trace = json.loads(changed_writer.read_ref(changed["manifest"]["trace"]))
    assert first_trace["source_refs"][0]["sha256"] != changed_trace["source_refs"][0]["sha256"]
    assert first["manifest"]["digest"] != changed["manifest"]["digest"]
    for manifest in (first["manifest"], changed["manifest"]):
        base = {key: value for key, value in manifest.items() if key != "digest"}
        assert hashlib.sha256(canonical_json_bytes(base)).hexdigest() == manifest["digest"]

    _expect_invalid(b"1\n\xff", block)


def test_csv_multiline_quoted_locator_and_layout_restriction_remain_intact():
    raw = b'ignored line\nsite,note\nA,"two\nlines"\nB,ok\n'
    _, normalized = _normalize(raw, _csv_block({"delimiter": ",", "header": "first_row", "skip_lines": 1}))
    obj = normalized["manifest"]["objects"][0]
    assert [item["locator"] for item in obj["source_locations"]] == ["lines 3-4", "line 5"]

    with pytest.raises(ContractError, match="CSV syntax is invalid"):
        _normalize(b'site,note\nA,"unfinished\n', _csv_block())
    _expect_invalid(
        b"site,note\nA,ok\n",
        _csv_block({"delimiter": ",", "header": "first_row", "skip_lines": 0, "take_rows": 1}),
    )


def test_csv_positions_are_zero_based_and_out_of_range_reports_row_width():
    raw = b"site,weight\nA,5\n"

    positional = _csv_block()
    positional["columns"] = [_column("site", 0), _column("weight", 1, "integer")]
    positional_writer, positional_result = _normalize(raw, positional)
    values_ref = positional_result["manifest"]["objects"][0]["values"]
    assert json.loads(positional_writer.read_ref(values_ref)) == [{"site": "A", "weight": 5}]

    one_based = dict(positional)
    one_based["columns"] = [_column("site", 1), _column("weight", 2, "integer")]
    error = _expect_invalid(raw, one_based, "/columns/weight")
    assert error.details == {"position": 2, "column_count": 2}

    named = _csv_block()
    named["columns"] = [
        _column("site", None, input_name="site"),
        _column("weight", None, "integer", input_name="weight"),
    ]
    named_writer, named_result = _normalize(raw, named)
    named_values_ref = named_result["manifest"]["objects"][0]["values"]
    assert json.loads(named_writer.read_ref(named_values_ref)) == [{"site": "A", "weight": 5}]
    short_row_error = _expect_invalid(b"site,weight\nA\n", named, "/columns/weight")
    assert short_row_error.details == {"position": 1, "column_count": 1}

    nullable = _csv_block()
    nullable["columns"] = [
        _column("site", None, input_name="site"),
        _column("optional", 2, "integer", nullable=True),
    ]
    nullable["missing_policy"] = "allow_nullable"
    nullable_writer, nullable_result = _normalize(raw, nullable)
    nullable_values_ref = nullable_result["manifest"]["objects"][0]["values"]
    assert json.loads(nullable_writer.read_ref(nullable_values_ref)) == [{"site": "A", "optional": None}]


def test_csv_skips_only_physical_blank_records_and_keeps_empty_field_records():
    _, blank_only = _normalize(b"site,note\n\n", _csv_block())
    assert blank_only["manifest"]["objects"][0]["values"]
    assert blank_only["manifest"]["objects"][0]["source_locations"] == []
    with pytest.raises(ContractError, match="missing value"):
        _normalize(b"site,note\n,\n", _csv_block())
