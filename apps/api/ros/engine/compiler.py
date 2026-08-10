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

import ros.nodes  # noqa: F401  (import registers all built-in node types)
from ros.engine.context import CompileContext
from ros.engine.expressions import ExpressionError, eval_expression
from ros.engine.node_io import (
    enforce_input_schema,
    enforce_output_schema,
    fold_edge_mappings,
    primary_output_key,
)
from ros.engine.registry import get_spec
from ros.engine.state import build_state_typeddict
from ros.nodes.flow import (
    make_fanout_path,
    make_router_path,
    resilient_fanout_child,
    router_targets,
    with_error_handling,
)

log = logging.getLogger("ros.compiler")


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

    # Edge data-mappings (WS8 c): collect the outgoing `mappings` per SOURCE node so the source's
    # fn can fold the mapped keys into its state update after it runs. Only plain data edges carry
    # mappings - skip sub-agent handles, branches (control routing), edges touching a folded
    # sub-agent child, and router/fanout sources (they route via config; their labeled edges are
    # ignored). The validator warns on a mapping placed on any of those (it won't apply).
    mappings_by_source: dict[str, list[dict]] = {}
    for e in definition.get("edges", []):
        if e.get("source_handle") == SUBAGENT_HANDLE or e.get("branches"):
            continue
        src = e["source"]
        if src in subagent_child_ids or e.get("target") in subagent_child_ids:
            continue
        if (node_by_id.get(src) or {}).get("type") in ("router", "parallel_fanout"):
            continue
        maps = e.get("mappings")
        if maps:
            mappings_by_source.setdefault(src, []).extend(maps)

    # 1) add every node from its registered factory (folded sub-agent children are not nodes)
    for n in nodes:
        if n["id"] in subagent_child_ids:
            continue
        spec = get_spec(n["type"])
        node_fn = spec.factory(n.get("config", {}) or {}, ctx)
        # Output-schema enforcement (WS8 a): validate the node's PRIMARY output value against its
        # declared output_schema. Applied INNERMOST so a strict violation surfaces before the
        # error_handling wrapper, letting on_error=continue/default absorb it. Skipped (with a
        # warning) when the node has no single structured output to validate.
        oschema = n.get("output_schema")
        if oschema:
            okey = primary_output_key(n["type"], n.get("config", {}) or {})
            if okey:
                node_fn = enforce_output_schema(
                    node_fn, schema=oschema, strict=bool(n.get("output_schema_strict")),
                    name=n["id"], key=okey,
                )
            else:
                log.warning(
                    "node %r declares output_schema but has no primary output value to validate; "
                    "skipping enforcement", n["id"],
                )
        # Input-schema enforcement (WS8): validate the incoming state against the node's
        # input_schema BEFORE it runs. Also innermost (inside error_handling) so a strict input
        # violation composes with on_error. Catches bad trigger/initial inputs and multi-source
        # state that output validation upstream can't.
        ischema = n.get("input_schema")
        if ischema:
            node_fn = enforce_input_schema(
                node_fn, schema=ischema, strict=bool(n.get("input_schema_strict")), name=n["id"],
            )
        fcfg = fanout_children.get(n["id"])
        if fcfg is not None:
            # Isolate per-item errors when the workflow opts into continue-on-error OR the
            # fanout sets on_item_error="skip"; bound each item with an optional per-item
            # timeout. Default (halt / no timeout) leaves the child untouched (safe).
            isolate = error_policy == "continue" or fcfg.get("on_item_error") == "skip"
            timeout = fcfg.get("item_timeout_seconds")
            if isolate or timeout:
                node_fn = resilient_fanout_child(node_fn, timeout=timeout, isolate=isolate)
        # Per-node retry + continue-on-fail (A/C10). Overrides the workflow error_policy for
        # this node; no-op when the node declares no error_handling.
        eh = n.get("error_handling")
        if eh:
            node_fn = with_error_handling(node_fn, eh)
        # Edge data-mappings (WS8 c): applied OUTERMOST so mapped keys augment whatever the node
        # (post error-handling) returned; the mapped keys ride shared state to the target.
        maps = mappings_by_source.get(n["id"])
        if maps:
            node_fn = fold_edge_mappings(node_fn, maps)
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
