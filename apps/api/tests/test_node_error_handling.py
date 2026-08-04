"""A/C10 - per-node retry & continue-on-fail.

Covers the error_handling schema (AC-1) and the runtime wrapper behavior (AC-2 retry /
non-retryable, AC-3 continue/default vs fail) with its observability metrics (AC-5). The
compiler wiring (with_error_handling applied to nodes that declare error_handling) is a 3-line
addition covered by the engine regression suite; the builder inspector (AC-4) is frontend.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from langgraph.errors import GraphBubbleUp

from ros.nodes.flow import with_error_handling
from ros.util.metrics import snapshot

_SCHEMAS = pathlib.Path(__file__).resolve().parents[3] / "packages" / "schemas" / "ros"


# --- AC-1: the error_handling schema fragment exists and is optional ----------------------

def test_common_defines_error_handling():
    common = json.loads((_SCHEMAS / "common.json").read_text())
    eh = common["$defs"]["ErrorHandling"]
    assert eh["properties"]["on_error"]["enum"] == ["fail", "continue", "default"]
    assert "retry" in eh["properties"]


def test_node_schema_references_error_handling_optional():
    wf = json.loads((_SCHEMAS / "workflow.json").read_text())
    node = wf["$defs"]["NodeInstance"]
    assert "error_handling" in node["properties"]
    assert "error_handling" not in node.get("required", [])


# --- AC-2: retry a transient failure, don't retry a non-retryable one ----------------------

async def test_retry_then_succeed():
    calls = {"n": 0}

    async def flaky(state):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return {"ok": True}

    wrapped = with_error_handling(flaky, {"retry": {"max_retries": 3, "initial_delay": 0}})
    before = snapshot().get("nodes.retry", 0)
    out = await wrapped({})
    assert out == {"ok": True}
    assert calls["n"] == 3  # 1 initial + 2 retries
    assert snapshot().get("nodes.retry", 0) >= before + 2  # AC-5 metric


async def test_non_retryable_not_retried():
    calls = {"n": 0}

    async def raise_key(state):
        calls["n"] += 1
        raise KeyError("k")

    # retry_on restricts to value_error -> a KeyError is NOT retried despite max_retries
    wrapped = with_error_handling(
        raise_key, {"retry": {"max_retries": 5, "initial_delay": 0, "retry_on": ["value_error"]}},
    )
    with pytest.raises(KeyError):
        await wrapped({})
    assert calls["n"] == 1


# --- AC-3: on_error fail (default) vs continue vs default ----------------------------------

async def test_exhausted_fail_raises():
    async def always_fail(state):
        raise RuntimeError("boom")

    wrapped = with_error_handling(always_fail, {"retry": {"max_retries": 1, "initial_delay": 0}})
    with pytest.raises(RuntimeError):
        await wrapped({})  # on_error defaults to 'fail'


async def test_continue_returns_empty_update():
    async def always_fail(state):
        raise RuntimeError("boom")

    wrapped = with_error_handling(always_fail, {"on_error": "continue"})
    before = snapshot().get("nodes.continue_on_fail", 0)
    out = await wrapped({})
    assert out == {}  # run continues with no state change
    assert snapshot().get("nodes.continue_on_fail", 0) >= before + 1  # AC-5 metric


async def test_default_returns_default_output():
    async def always_fail(state):
        raise ValueError("boom")

    wrapped = with_error_handling(
        always_fail, {"on_error": "default", "default_output": {"fallback": True}},
    )
    assert await wrapped({}) == {"fallback": True}


# --- control-flow signals (interrupts / cancellation) are never swallowed ------------------

async def test_control_flow_not_swallowed_by_continue():
    async def interrupts(state):
        raise GraphBubbleUp()

    wrapped = with_error_handling(interrupts, {"on_error": "continue"})
    with pytest.raises(GraphBubbleUp):
        await wrapped({})  # HITL / Command bubbling must reach the graph
