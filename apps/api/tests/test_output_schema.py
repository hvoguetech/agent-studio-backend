"""C/B1 - typed tool/node output_schema: the schema field (AC-1), runtime validation (AC-2),
and draft inference (AC-4). Builder autocomplete + mapping validation (AC-3) is frontend."""

from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

from forge.tools.output_schema import OutputSchemaError, infer_schema, validate_output
from forge.util.metrics import snapshot

_SCHEMAS = pathlib.Path(__file__).resolve().parents[3] / "packages" / "schemas" / "forge"


# --- AC-1: the tool + node schemas accept an OPTIONAL output_schema ------------------------

def test_tool_schema_accepts_optional_output_schema():
    tool = json.loads((_SCHEMAS / "tool.json").read_text())
    props = tool["properties"]
    assert "output_schema" in props
    assert props["output_schema"]["type"] == "object"
    assert "output_schema" not in tool.get("required", [])  # optional: a def omitting it validates


def test_node_schema_accepts_optional_output_schema():
    wf = json.loads((_SCHEMAS / "workflow.json").read_text())
    node = wf["$defs"]["NodeInstance"]
    assert "output_schema" in node["properties"]
    assert "output_schema" not in node.get("required", [])


# --- AC-2: runtime validation of the PROJECTED output -------------------------------------

_SCHEMA = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}


def test_validate_output_no_schema_is_noop():
    assert validate_output({"anything": 1}, None) is None
    assert validate_output({"anything": 1}, {}) is None


def test_validate_output_match_returns_none():
    assert validate_output({"id": 7}, _SCHEMA) is None


def test_validate_output_observe_records_mismatch():
    before = snapshot().get("tools.output_schema_mismatch", 0)
    msg = validate_output({"id": "not-an-int"}, _SCHEMA, strict=False, name="t1")
    assert msg  # returns the error message and does NOT raise
    assert snapshot().get("tools.output_schema_mismatch", 0) >= before + 1  # metric recorded


def test_validate_output_strict_raises():
    with pytest.raises(OutputSchemaError):
        validate_output({"id": "not-an-int"}, _SCHEMA, strict=True, name="t2")


def test_validate_output_bad_schema_is_noop():
    # a malformed schema is an authoring error, not an output failure
    assert validate_output({"id": 1}, {"type": "not-a-real-type"}) is None


# --- AC-4: inference yields a valid, round-tripping draft schema ---------------------------

def test_infer_schema_roundtrips():
    sample = {"id": 1, "name": "x", "ok": True, "tags": ["a", "b"], "nested": {"k": 1.5}}
    schema = infer_schema(sample)
    jsonschema.Draft202012Validator.check_schema(schema)  # itself a valid JSON Schema
    jsonschema.validate(sample, schema)  # and it accepts the sample it was inferred from
    assert schema.get("type") == "object"


def test_infer_schema_primitives():
    assert infer_schema("hi")["type"] == "string"
    assert infer_schema(3)["type"] == "integer"
    assert infer_schema(True)["type"] == "boolean"  # bool before int
    assert infer_schema([{"a": 1}])["type"] == "array"
