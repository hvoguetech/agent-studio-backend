"""WS8 typed node I/O: runtime output_schema enforcement, edge data-mappings, and the
build-time contract checks (mapping validation + producer->consumer field/presence contract)."""

from __future__ import annotations

import copy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ros.engine.compiler import compile_workflow
from ros.services.runtime import make_runtime_ctx
from ros.services.validation import validate_workflow

# --- runtime (compiler) ---------------------------------------------------------------------

_LINEAR = {
    "id": "wf_io", "version": 1,
    "state": {
        "messages": {"type": "list[message]", "reducer": "add_messages"},
        "payload": {"type": "json", "reducer": "last"},
        "data": {"type": "json", "reducer": "last"},
        "chosen": {"type": "str", "reducer": "last"},
    },
    "entry_node": "start",
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {"id": "xf", "type": "transform", "config": {"expression": "payload", "output_key": "data"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [
        {"source": "start", "target": "xf"},
        {"source": "xf", "target": "end"},
    ],
}


def _compiled(wf):
    ctx = make_runtime_ctx("t_io", "p_io")
    ctx.checkpointer = InMemorySaver()
    return compile_workflow(wf, ctx)


async def test_edge_mapping_copies_field_across_edge():
    wf = copy.deepcopy(_LINEAR)
    wf["edges"][1]["mappings"] = [{"from": "data.value", "to": "chosen"}]
    out = await _compiled(wf).ainvoke({"payload": {"value": "hello"}}, {"configurable": {"thread_id": "m1"}})
    assert out["data"] == {"value": "hello"}
    assert out["chosen"] == "hello"  # mapping lifted output.data.value into the `chosen` channel


async def test_output_schema_observe_does_not_raise():
    wf = copy.deepcopy(_LINEAR)
    wf["nodes"][1]["output_schema"] = {"type": "string"}  # but `data` is a dict -> mismatch
    out = await _compiled(wf).ainvoke({"payload": {"a": 1}}, {"configurable": {"thread_id": "m2"}})
    assert out["data"] == {"a": 1}  # observe mode: logged + metric, run continues


async def test_output_schema_strict_raises():
    wf = copy.deepcopy(_LINEAR)
    wf["nodes"][1]["output_schema"] = {"type": "string"}
    wf["nodes"][1]["output_schema_strict"] = True
    with pytest.raises(Exception) as ei:  # noqa: PT011 - LangGraph may wrap; assert on message
        await _compiled(wf).ainvoke({"payload": {"a": 1}}, {"configurable": {"thread_id": "m3"}})
    assert "output_schema" in str(ei.value)


async def test_output_schema_strict_absorbed_by_on_error():
    wf = copy.deepcopy(_LINEAR)
    wf["nodes"][1]["output_schema"] = {"type": "string"}
    wf["nodes"][1]["output_schema_strict"] = True
    wf["nodes"][1]["error_handling"] = {"on_error": "continue"}  # strict raise -> absorbed
    out = await _compiled(wf).ainvoke({"payload": {"a": 1}}, {"configurable": {"thread_id": "m4"}})
    assert out.get("data") is None  # node continued with an empty update; run completed


# --- build-time contract (validator) --------------------------------------------------------


def test_valid_mapping_passes():
    wf = copy.deepcopy(_LINEAR)
    wf["edges"][1]["mappings"] = [{"from": "data.value", "to": "chosen"}]
    res = validate_workflow(wf)
    assert res.valid, res.errors


def test_mapping_to_undeclared_key_errors():
    wf = copy.deepcopy(_LINEAR)
    wf["edges"][1]["mappings"] = [{"from": "data", "to": "nope"}]
    res = validate_workflow(wf)
    assert not res.valid
    assert any("nope" in e["message"] and "/mappings/0/to" in e["pointer"] for e in res.errors), res.errors


def test_mapping_from_invalid_jmespath_errors():
    wf = copy.deepcopy(_LINEAR)
    wf["edges"][1]["mappings"] = [{"from": "data[", "to": "chosen"}]
    res = validate_workflow(wf)
    assert not res.valid
    assert any("/mappings/0/from" in e["pointer"] for e in res.errors), res.errors


def test_mapping_on_branch_edge_warns():
    wf = copy.deepcopy(_LINEAR)
    wf["edges"][1] = {
        "source": "xf", "target": "end", "condition": "chosen",
        "branches": {"go": "end"}, "mappings": [{"from": "data", "to": "chosen"}],
    }
    res = validate_workflow(wf)
    assert any("ignored" in w["message"] and "/mappings" in w["pointer"] for w in res.warnings), res.warnings


def test_input_schema_undeclared_required_errors():
    wf = copy.deepcopy(_LINEAR)
    wf["nodes"][1]["input_schema"] = {"type": "object", "required": ["ghost"],
                                      "properties": {"ghost": {"type": "string"}}}
    res = validate_workflow(wf)
    assert not res.valid
    assert any("ghost" in e["message"] and "not a declared State field" in e["message"] for e in res.errors)


def test_input_schema_unwritten_required_warns():
    wf = copy.deepcopy(_LINEAR)
    # `chosen` is a declared channel but no node/mapping writes it -> a soft warning, not an error.
    wf["nodes"][1]["input_schema"] = {"type": "object", "required": ["chosen"],
                                      "properties": {"chosen": {"type": "string"}}}
    res = validate_workflow(wf)
    assert res.valid, res.errors
    assert any("chosen" in w["message"] and "no node or edge mapping" in w["message"] for w in res.warnings)


def test_mapping_type_mismatch_errors():
    wf = copy.deepcopy(_LINEAR)
    wf["nodes"][1]["output_schema"] = {"type": "object", "properties": {"n": {"type": "string"}}}
    wf["nodes"][2]["input_schema"] = {"type": "object", "properties": {"chosen": {"type": "integer"}}}
    wf["edges"][1]["mappings"] = [{"from": "data.n", "to": "chosen"}]
    res = validate_workflow(wf)
    assert not res.valid
    assert any("incompatible types" in e["message"] for e in res.errors), res.errors


def test_malformed_output_schema_warns():
    wf = copy.deepcopy(_LINEAR)
    wf["nodes"][1]["output_schema"] = {"type": 123}  # not a valid JSON Schema
    res = validate_workflow(wf)
    assert any("not a valid JSON Schema" in w["message"] for w in res.warnings), res.warnings
