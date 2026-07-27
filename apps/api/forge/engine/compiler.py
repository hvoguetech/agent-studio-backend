"""The workflow compiler (Doc 2 §6): executable JSON -> `CompiledStateGraph`.

Topologically agnostic - it trusts the validator (schemas/workflow.json + extra
rules) to have already rejected bad definitions. Routing:

- `router` nodes route via their own `config.cases`/`default` (conditional edges);
  their labeled out-edges in `edges[]` are ignored.
- edges with `branches` (value->target) become conditional edges keyed by an
  optional `condition` expression.
- `end` nodes are wired to END; plain edges are added as-is.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

import forge.nodes  # noqa: F401  (import registers all built-in node types)
from forge.engine.context import CompileContext
from forge.engine.expressions import ExpressionError, eval_expression
from forge.engine.registry import get_spec
from forge.engine.state import build_state_typeddict
from forge.nodes.flow import (
    make_fanout_path,
    make_router_path,
    resilient_fanout_child,
    router_targets,
)

log = logging.getLogger("forge.compiler")


def _branch_path(condition: str | None, mapping: dict[str, str], source: str = "?"):
    def _path(state: dict) -> Any:
        if not condition:
            # A branches-edge with no condition can only ever fall through to END. The
            # validator now errors on this (audit F10c); if one still slips through, make the
            # dead-end visible rather than silently ending the run (mirrors flow.py routers).
            log.warning("edge from %r has branches but no condition; routing to END", source)
            return END
        try:
            val = str(eval_expression(condition, dict(state or {})))
        except ExpressionError as e:
            # A failing branch expression silently routing to END is a debugging nightmare;
            # log it (mirrors make_router_path in flow.py) so it's traceable (audit F8).
            log.warning("edge %r branch condition %r failed: %s", source, condition, e)
            return END
        if val in mapping:
            return mapping[val]
        log.warning("edge %r branch value %r matched no branch; routing to END", source, val)
        return END

    return _path


SUBAGENT_HANDLE = "subagents"


def _subagent_spec_from_node(node: dict) -> dict:
    """Turn a specialist agent NODE's config into the subagent dict `build_subagents` expects
    (name/description/system_prompt/tools/toolsets/model/middleware). Lets a deep_agent's
    sub-agents be authored as full agent nodes on the canvas rather than inline JSON."""
    cfg = node.get("config", {}) or {}
    name = cfg.get("name") or node["id"]
    spec: dict[str, Any] = {
        "name": name,
        # The supervisor reads this to decide when to call the sub-agent (like a tool description).
        "description": cfg.get("description") or f"The {name} specialist agent.",
        # deepagents requires every sub-agent to carry a system_prompt (it wraps it with a
        # profile prompt), so always provide one - fall back to the description / a generic.
        "system_prompt": cfg.get("system_prompt") or cfg.get("description") or f"You are the {name} specialist agent.",
    }
    for k in ("tools", "toolsets", "model", "middleware"):
        if cfg.get(k):
            spec[k] = cfg[k]
    return spec


def compile_workflow(definition: dict, ctx: CompileContext):
    """Compile an executable workflow definition into a runnable LangGraph graph."""
    state_schema = build_state_typeddict(definition.get("state", {}))
    builder = StateGraph(state_schema)

    nodes = definition["nodes"]
    node_by_id = {n["id"]: n for n in nodes}

    # Sub-agent edges (source_handle == "subagents") wire a deep_agent to specialist agent nodes
    # it can call as tools. Those child nodes are NOT standalone graph nodes: fold each child's
    # config into the parent deep_agent's `subagents` (create_deep_agent picks them up in
    # agent_factory) and skip the child + its edges below. This is the canvas supervisor pattern.
    subagent_child_ids: set[str] = set()
    subagent_by_parent: dict[str, list[str]] = {}
    for e in definition.get("edges", []):
        if e.get("source_handle") != SUBAGENT_HANDLE:
            continue
        parent, child = e["source"], e.get("target")
        if child in node_by_id and (node_by_id.get(parent) or {}).get("type") == "deep_agent":
            subagent_by_parent.setdefault(parent, []).append(child)
            subagent_child_ids.add(child)
    for parent_id, child_ids in subagent_by_parent.items():
        parent = node_by_id[parent_id]
        cfg = dict(parent.get("config") or {})
        cfg["subagents"] = list(cfg.get("subagents") or []) + [
            _subagent_spec_from_node(node_by_id[c]) for c in child_ids
        ]
        parent["config"] = cfg

    # A parallel_fanout dispatches one Send per item to its `child_node` (all run in one
    # superstep). Map each such child id -> its fanout config so we can optionally harden the
    # child against a single item's failure/timeout below (audit F2). The workflow-level
    # error_policy "continue" is the opt-in for partial-failure isolation.
    error_policy = definition.get("error_policy", "halt")
    fanout_children: dict[str, dict] = {}
    for n in nodes:
        if n["type"] == "parallel_fanout":
            fcfg = n.get("config", {}) or {}
            if fcfg.get("child_node"):
                fanout_children[fcfg["child_node"]] = fcfg

    # 1) add every node from its registered factory (folded sub-agent children are not nodes)
    for n in nodes:
        if n["id"] in subagent_child_ids:
            continue
        spec = get_spec(n["type"])
        node_fn = spec.factory(n.get("config", {}) or {}, ctx)
        fcfg = fanout_children.get(n["id"])
        if fcfg is not None:
            # Isolate per-item errors when the workflow opts into continue-on-error OR the
            # fanout sets on_item_error="skip"; bound each item with an optional per-item
            # timeout. Default (halt / no timeout) leaves the child untouched (safe).
            isolate = error_policy == "continue" or fcfg.get("on_item_error") == "skip"
            timeout = fcfg.get("item_timeout_seconds")
            if isolate or timeout:
                node_fn = resilient_fanout_child(node_fn, timeout=timeout, isolate=isolate)
        builder.add_node(n["id"], node_fn)

    # 2) terminal markers -> END
    for n in nodes:
        if n["type"] == "end":
            builder.add_edge(n["id"], END)

    # 3) router nodes -> conditional edges from their own config
    routed: set[str] = set()
    for n in nodes:
        if n["type"] == "router":
            cfg = n.get("config", {}) or {}
            targets = router_targets(cfg) or [END]
            builder.add_conditional_edges(n["id"], make_router_path(cfg), targets)
            routed.add(n["id"])

    # 3b) parallel_fanout nodes -> Send-based map to their child_node (skip normal edges)
    for n in nodes:
        if n["type"] == "parallel_fanout":
            cfg = n.get("config", {}) or {}
            child = cfg.get("child_node")
            if child:
                builder.add_conditional_edges(n["id"], make_fanout_path(cfg), [child])
                routed.add(n["id"])

    # 4) explicit edges (skip self-routing router/fanout sources, sub-agent edges, and any edge
    #    touching a folded sub-agent child - the child is no longer a node in the graph)
    for e in definition.get("edges", []):
        src = e["source"]
        if e.get("source_handle") == SUBAGENT_HANDLE:
            continue
        if src in routed:
            continue
        if src in subagent_child_ids or e.get("target") in subagent_child_ids:
            continue
        if e.get("branches"):
            mapping = {str(k): v for k, v in e["branches"].items()}
            # END must be in the target set: _branch_path falls through to END on a failed /
            # unmatched condition, and without END listed LangGraph raises KeyError('__end__')
            # at runtime instead of the intended (now-logged) graceful end (audit F8).
            targets = sorted(set(mapping.values()) | {END})
            builder.add_conditional_edges(
                src, _branch_path(e.get("condition"), mapping, src), targets
            )
        else:
            tgt = e["target"]
            builder.add_edge(src, END if tgt in ("END", "__end__") else tgt)

    # 5) entry
    builder.add_edge(START, definition["entry_node"])

    return builder.compile(checkpointer=ctx.checkpointer, store=ctx.store)
