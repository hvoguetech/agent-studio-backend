"""WS8 typed node I/O helpers, shared by the compiler (runtime output-schema enforcement +
edge data-mapping) and the validator (build-time contract checks).

Data flows between nodes through the shared LangGraph state, not through typed ports. A node's
"primary output" is the single structured value it writes to its `output_key` (or the
conventional `structured_response` channel for structured llm/agent output). `primary_output_key`
names that state key so the runtime validator and the build-time contract check look at the
same value. `apply_edge_mappings` implements the edge `mappings` contract: copy a JMESPath over
the source node's output + current state into a declared target state key.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import jmespath

log = logging.getLogger("ros.node_io")


def primary_output_key(node_type: str | None, config: dict | None) -> str | None:
    """The state key holding a node's PRIMARY structured output value, or None when the node
    has no single structured output to validate/map (e.g. an agent that only writes messages).

    Mirrors each node factory's write in ros/nodes/* - keep in sync when a node's output key
    convention changes."""
    cfg = config or {}
    if node_type == "classifier":
        return cfg.get("output_key", "intent")
    if node_type == "transform":
        return cfg.get("output_key", "data")
    if node_type == "tool_call":
        return cfg.get("output_key", "tool_result")
    if node_type == "webhook_out":
        return cfg.get("output_key", "webhook_result")
    if node_type == "human_input":
        return cfg.get("output_key") or None
    if node_type == "retrieval":
        return cfg.get("route_key") or None
    if node_type == "join":
        return cfg.get("output_key") or cfg.get("input_key") or None
    if node_type in ("llm", "agent", "deep_agent"):
        # Structured output lands on the conventional `structured_response` channel; a plain
        # (messages-only) call has no single structured value to validate.
        rf = cfg.get("response_format") or {}
        if rf.get("mode") == "structured" and rf.get("schema"):
            return "structured_response"
        return None
    return None


def _search(expr: str, data: dict) -> Any:
    try:
        return jmespath.search(expr, data)
    except jmespath.exceptions.JMESPathError as e:  # malformed at author time; validator flags it
        log.warning("edge mapping expression %r failed: %s: %s", expr, type(e).__name__, e)
        return None


def apply_edge_mappings(
    mappings: list[dict], node_update: dict, state: dict,
) -> dict[str, Any]:
    """Compute the state keys an edge's `mappings` write, AFTER the source node ran.

    Each mapping's `from` is a JMESPath evaluated over a merged view of the current run state
    and the node's fresh output update, with the output also exposed under `output` so authors
    can write either `output.<field>` or a bare state key. Returns only the mapped `{to: value}`
    pairs (a partial state update to merge in). A mapping whose `to` is falsy is skipped."""
    if not mappings:
        return {}
    merged = {**(state or {}), **(node_update or {}), "output": dict(node_update or {})}
    out: dict[str, Any] = {}
    for m in mappings:
        if not isinstance(m, dict):
            continue
        src, dst = m.get("from"), m.get("to")
        if not src or not dst:
            continue
        out[dst] = _search(src, merged)
    return out


def bind_invoke(fn):
    """Return an async `(state, config) -> result` callable that invokes `fn` whether it's a
    LangGraph Runnable (compiled subgraph), an async fn, or a plain sync fn, passing the optional
    2nd `config` arg only when the fn accepts it. Matches the invocation contract used by the
    other node-fn wrappers in ros/nodes/flow.py."""
    is_runnable = hasattr(fn, "ainvoke")
    is_coro = inspect.iscoroutinefunction(fn)
    accepts_config = False
    if not is_runnable:
        try:
            accepts_config = len(inspect.signature(fn).parameters) >= 2
        except (TypeError, ValueError):
            accepts_config = False

    async def _invoke(state, config):
        if is_runnable:
            return await fn.ainvoke(state, config)
        if is_coro:
            return await (fn(state, config) if accepts_config else fn(state))
        return fn(state, config) if accepts_config else fn(state)

    return _invoke


def enforce_output_schema(fn, *, schema: dict, strict: bool, name: str, key: str):
    """WS8 (a): wrap a node fn so its PRIMARY output value is validated against the node's
    declared `output_schema`. Observe/warn by default (logs + a `nodes.output_schema_mismatch`
    metric); strict RAISES `OutputSchemaError`, which propagates to the node's error_handling
    wrapper (so on_error=continue/default can absorb it). Validation is SKIPPED when the node
    didn't write `key` this run (it produced nothing to check)."""
    from ros.tools.output_schema import validate_output

    invoke = bind_invoke(fn)

    async def _wrapped(state: dict, config=None) -> Any:
        update = await invoke(state, config)
        if isinstance(update, dict) and key in update:
            validate_output(
                update[key], schema, strict=strict, name=name,
                metric="nodes.output_schema_mismatch",
            )
        return update

    return _wrapped


def enforce_input_schema(fn, *, schema: dict, strict: bool, name: str):
    """WS8: validate a node's INPUT (the incoming shared state, projected to the keys its
    `input_schema` mentions) BEFORE it runs. Observe/warn by default (logs + a
    `nodes.input_schema_mismatch` metric); strict RAISES `OutputSchemaError` before the node
    executes, which propagates to the node's error_handling wrapper. Projecting to the schema's
    own keys keeps `additionalProperties:false` schemas from tripping on unrelated state channels
    (messages, loop counters, …); a missing REQUIRED key still fails via the schema's `required`."""
    from ros.tools.output_schema import validate_output

    invoke = bind_invoke(fn)
    props = schema.get("properties") if isinstance(schema, dict) else None
    keys = set(props or {}) | set(schema.get("required") or [] if isinstance(schema, dict) else [])

    async def _wrapped(state: dict, config=None) -> Any:
        if isinstance(state, dict) and keys:
            projected = {k: state[k] for k in keys if k in state}
            validate_output(
                projected, schema, strict=strict, name=name,
                metric="nodes.input_schema_mismatch", noun="input", label="input_schema",
            )
        return await invoke(state, config)

    return _wrapped


def fold_edge_mappings(fn, mappings: list[dict]):
    """WS8 (c): wrap a source node fn so, after it runs, the outgoing edges' `mappings` are
    applied and merged into its state update. Mapped keys ride the shared state to whatever
    target consumes them next; a `to` that isn't a declared channel is dropped by LangGraph
    (the validator errors on that at build time)."""
    invoke = bind_invoke(fn)

    async def _wrapped(state: dict, config=None) -> Any:
        update = await invoke(state, config)
        if not isinstance(update, dict):
            return update
        mapped = apply_edge_mappings(mappings, update, state)
        if mapped:
            return {**update, **mapped}
        return update

    return _wrapped
