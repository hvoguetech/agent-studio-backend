"""Transpile a workflow's executable JSON into a readable, standalone LangGraph `graph.py`
(WS8 "Export for LangGraph Studio", hybrid mode).

The emitted file builds an EXPLICIT `StateGraph` — every `add_node` / `add_edge` /
`add_conditional_edges` is literal Python you can read, reorder, and edit — instead of a
`compile_workflow(json)` black box. Node BODIES are inlined for the graph-logic + LLM nodes
(`start`/`end`/`transform`/`llm`); the nodes whose behaviour lives in the ROS engine — `agent`/
`deep_agent`/`classifier`/`tool_call`/`retrieval`/`subworkflow`/`webhook_out`/`emit_event`/
`human_input`/`handoff`/`loop`/`join` — delegate to a thin `_ros(type, config)` adapter that builds
them from their registered factory (so agent middleware, materialized tools, and RAG stay faithful).

Deliberately NOT reproduced in the exported graph (they're runtime-engine concerns, noted in a
comment in the file): per-node retry / error_policy / fanout isolation and WS8 output/input schema
enforcement.
"""

from __future__ import annotations

import re

# Node types whose body we inline as plain LangGraph Python. Everything else is ROS-backed.
_INLINE = {"start", "end", "transform", "llm"}
_END_TOKENS = {"END", "__end__"}
_SUBAGENT = "subagents"

# state type -> python type used for the channel (mirrors ros.engine.state.PY_TYPES).
_PY = {
    "list[message]": "list", "list[str]": "list", "list[json]": "list",
    "str": "str", "int": "int", "float": "float", "bool": "bool", "json": "dict",
}


def _ident(node_id: str) -> str:
    return "_n_" + re.sub(r"\W", "_", node_id)


def _lit(value) -> str:
    return repr(value)


def _fold_subagents(nodes_by_id: dict, edges: list) -> tuple[set, dict]:
    """Mirror compiler: fold deep_agent specialist children into the parent's config.subagents;
    the children are not standalone graph nodes. Returns (child_ids, parent_id -> folded_config)."""
    child_ids: set[str] = set()
    by_parent: dict[str, list[str]] = {}
    for e in edges:
        if e.get("source_handle") != _SUBAGENT:
            continue
        parent, child = e.get("source"), e.get("target")
        if child in nodes_by_id and (nodes_by_id.get(parent) or {}).get("type") == "deep_agent":
            by_parent.setdefault(parent, []).append(child)
            child_ids.add(child)
    folded: dict[str, dict] = {}
    for parent_id, children in by_parent.items():
        cfg = dict(nodes_by_id[parent_id].get("config") or {})
        subs = list(cfg.get("subagents") or [])
        for c in children:
            ccfg = nodes_by_id[c].get("config", {}) or {}
            name = ccfg.get("name") or c
            spec = {
                "name": name,
                "description": ccfg.get("description") or f"The {name} specialist agent.",
                "system_prompt": ccfg.get("system_prompt") or ccfg.get("description") or f"You are the {name} specialist agent.",
            }
            for k in ("tools", "toolsets", "model", "middleware"):
                if ccfg.get(k):
                    spec[k] = ccfg[k]
            subs.append(spec)
        cfg["subagents"] = subs
        folded[parent_id] = cfg
    return child_ids, folded


def _default_state(state_cfg: dict) -> dict:
    """The engine's implicit channels (ros.engine.state.build_state_typeddict) — keep in sync."""
    cfg = dict(state_cfg or {})
    cfg.setdefault("messages", {"type": "list[message]", "reducer": "add_messages"})
    cfg.setdefault("artifacts", {"type": "list[json]", "reducer": "artifacts"})
    return cfg


def _state_lines(state_cfg: dict) -> list[str]:
    out: list[str] = ["class State(TypedDict, total=False):"]
    for field, spec in _default_state(state_cfg).items():
        py = _PY.get((spec or {}).get("type", "json"), "Any")
        reducer = (spec or {}).get("reducer", "last")
        if reducer == "add_messages":
            out.append(f"    {field}: Annotated[{py}, add_messages]")
        elif reducer == "add":
            out.append(f"    {field}: Annotated[{py}, operator.add]")
        elif reducer == "merge":
            out.append(f"    {field}: Annotated[{py}, _merge]")
        elif reducer == "artifacts":
            out.append(f"    {field}: Annotated[{py}, _merge_artifacts]")
        else:  # last / overwrite
            out.append(f"    {field}: {py}")
    return out


def _inline_transform(nid: str, cfg: dict) -> list[str]:
    ik, ok = cfg.get("input_key"), cfg.get("output_key", "data")
    expr = cfg.get("expression", "@")
    src = f"state.get({_lit(ik)})" if ik else "dict(state)"
    return [
        f"def {_ident(nid)}(state):",
        f"    src = {src}",
        "    try:",
        f"        result = jmespath.search({_lit(expr)}, src)",
        "    except Exception:",
        "        result = None",
        f"    return {{{_lit(ok)}: result}}",
    ]


def _inline_llm(nid: str, cfg: dict) -> list[str]:
    prompt = cfg.get("prompt")
    rf = cfg.get("response_format") or {}
    structured = rf.get("mode") == "structured" and rf.get("schema")
    lines = [
        f"async def {_ident(nid)}(state):",
        "    from langchain.chat_models import init_chat_model",
        "    from langchain_core.messages import SystemMessage",
        f"    model_ref = {_lit(cfg.get('model'))} or os.environ.get('ROS_DEFAULT_MODEL')",
        "    if not model_ref:",
        f"        raise RuntimeError('node {nid}: no model set — configure the node model or ROS_DEFAULT_MODEL')",
        "    llm = init_chat_model(model_ref)",
        "    msgs = list(state.get('messages') or [])",
        f"    rendered = _render({_lit(prompt)}, state) if {_lit(bool(prompt))} else None",
        "    inp = ([SystemMessage(content=rendered)] if rendered else []) + msgs",
    ]
    if structured:
        lines += [
            f"    result = await llm.with_structured_output({_lit(rf.get('schema'))}).ainvoke(inp)",
            "    return {'structured_response': result}",
        ]
    else:
        lines += [
            "    result = await llm.ainvoke(inp)",
            "    return {'messages': [result]}",
        ]
    return lines


def transpile(executable: dict, *, name: str | None = None) -> str:
    ex = executable or {}
    nodes = ex.get("nodes", [])
    edges = ex.get("edges", [])
    by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}
    entry = ex.get("entry_node") or (nodes[0]["id"] if nodes else "start")
    display = name or ex.get("id") or "workflow"

    child_ids, folded = _fold_subagents(by_id, edges)
    live_nodes = [n for n in nodes if n.get("id") not in child_ids]

    def cfg_of(n: dict) -> dict:
        return folded.get(n["id"]) or n.get("config") or {}

    state_cfg = ex.get("state", {})
    reducers_used = {(s or {}).get("reducer") for s in _default_state(state_cfg).values()}
    routed = {n["id"] for n in live_nodes if n.get("type") in ("router", "parallel_fanout")}
    has_ros = any(n.get("type") not in _INLINE for n in live_nodes)
    has_branches = any(isinstance(e, dict) and e.get("branches") for e in edges)
    has_router = any(n.get("type") == "router" for n in live_nodes)
    has_fanout = any(n.get("type") == "parallel_fanout" for n in live_nodes)
    has_transform = any(n.get("type") == "transform" and (cfg_of(n).get("engine", "jmespath") == "jmespath") for n in live_nodes)
    has_llm = any(n.get("type") == "llm" for n in live_nodes)
    has_subworkflow = any(n.get("type") == "subworkflow" for n in live_nodes)

    L: list[str] = []
    L.append(f'"""Auto-generated LangGraph for ROS workflow: "{display}".')
    L.append("")
    L.append("Explicit StateGraph (edit freely). start/end/transform/llm are inlined; agent/tool/")
    L.append("retrieval/etc. delegate to the ROS engine via _ros(). Re-export to regenerate.")
    L.append("NOTE: per-node retry/error_policy/fanout-isolation and WS8 schema enforcement are")
    L.append("runtime-engine features and are NOT applied in this exported graph.")
    L.append('"""')
    L.append("from __future__ import annotations")
    L.append("")
    L.append("import os")
    if has_llm:
        L.append("import re")
    if "add" in reducers_used:
        L.append("import operator")
    if has_transform:
        L.append("import jmespath")
    L.append("from typing import Annotated, Any, TypedDict")
    L.append("")
    L.append("from langgraph.graph import END, START, StateGraph")
    L.append("from langgraph.graph.message import add_messages")
    if has_router or has_fanout:
        L.append("from ros.nodes.flow import make_fanout_path, make_router_path, router_targets")
    if has_branches:
        L.append("from ros.engine.expressions import eval_expression")
    L.append("")
    L.append("")

    # ---- state ----
    if "merge" in reducers_used:
        L.append("def _merge(a, b):")
        L.append("    return {**(a or {}), **(b or {})}")
        L.append("")
        L.append("")
    if "artifacts" in reducers_used:
        # Mirrors ros.engine.state._merge_artifacts: append, updating in place by (bucket, key).
        L.append("def _merge_artifacts(a, b):")
        L.append("    def _ident(e):")
        L.append("        return (e.get('bucket'), e['key']) if isinstance(e, dict) and e.get('key') else None")
        L.append("    def _lst(v):")
        L.append("        return [] if v is None else (list(v) if isinstance(v, list) else [v])")
        L.append("    out = _lst(a)")
        L.append("    index = {}")
        L.append("    for i, e in enumerate(out):")
        L.append("        ident = _ident(e)")
        L.append("        if ident is not None:")
        L.append("            index.setdefault(ident, i)")
        L.append("    for e in _lst(b):")
        L.append("        ident = _ident(e)")
        L.append("        if ident is not None and ident in index:")
        L.append("            out[index[ident]] = e")
        L.append("        else:")
        L.append("            if ident is not None:")
        L.append("                index[ident] = len(out)")
        L.append("            out.append(e)")
        L.append("    return out")
        L.append("")
        L.append("")
    L += _state_lines(state_cfg)
    L.append("")
    L.append("")

    # ---- helpers ----
    if has_llm:
        L.append("def _render(template, state):")
        L.append("    # Minimal {{state.x}} / {{x}} substitution (approximation of the ROS templater).")
        L.append("    if not isinstance(template, str):")
        L.append("        return template")
        L.append("    def _sub(m):")
        L.append("        key = m.group(1).strip()")
        L.append("        key = key[6:] if key.startswith('state.') else key")
        L.append("        val = state.get(key)")
        L.append("        return '' if val is None else str(val)")
        L.append(r"    return re.sub(r'\{\{(.*?)\}\}', _sub, template)")
        L.append("")
        L.append("")
    if has_branches:
        L.append("def _branch(condition, mapping):")
        L.append("    def _path(state):")
        L.append("        if not condition:")
        L.append("            return END")
        L.append("        try:")
        L.append("            val = str(eval_expression(condition, dict(state)))")
        L.append("        except Exception:")
        L.append("            return END")
        L.append("        return mapping.get(val, END)")
        L.append("    return _path")
        L.append("")
        L.append("")
    if has_ros:
        if has_subworkflow:
            L.append("import json as _json")
            L.append("import pathlib as _pathlib")
            L.append("_WF_FILE = _pathlib.Path(__file__).parent / 'executable.json'")
            L.append("try:")
            L.append("    _EX = _json.loads(_WF_FILE.read_text(encoding='utf-8'))")
            L.append("    _WORKFLOWS = {(_EX.get('id') or 'workflow'): _EX}")
            L.append("except Exception:")
            L.append("    _WORKFLOWS = {}")
        else:
            L.append("_WORKFLOWS = {}")
        L.append("")
        L.append("")
        L.append("def _ros_ctx():")
        L.append("    from ros.engine.context import CompileContext")
        L.append("    return CompileContext(tenant_id='local', project_id='local',")
        L.append("                          default_model=os.environ.get('ROS_DEFAULT_MODEL'), workflows=_WORKFLOWS)")
        L.append("")
        L.append("")
        L.append("def _ros(node_type, config):")
        L.append('    """ROS-backed node — its behaviour (agent middleware / materialized tools / RAG) lives in')
        L.append("    the ROS engine and can't be inlined as plain LangGraph. Built from the registered factory.")
        L.append("    tool_call/retrieval/subworkflow degrade offline (no project tools/KB/siblings).\"\"\"")
        L.append("    from ros.engine.registry import get_spec")
        L.append("    return get_spec(node_type).factory(config, _ros_ctx())")
        L.append("")
        L.append("")

    # ---- inlined node bodies ----
    add_exprs: dict[str, str] = {}
    for n in live_nodes:
        nid, t, cfg = n["id"], n.get("type"), cfg_of(n)
        if t in ("start", "end"):
            L.append(f"def {_ident(nid)}(state):")
            L.append("    return {}")
            L.append("")
            L.append("")
            add_exprs[nid] = _ident(nid)
        elif t == "transform" and cfg.get("engine", "jmespath") == "jmespath":
            L += _inline_transform(nid, cfg)
            L.append("")
            L.append("")
            add_exprs[nid] = _ident(nid)
        elif t == "llm":
            L += _inline_llm(nid, cfg)
            L.append("")
            L.append("")
            add_exprs[nid] = _ident(nid)
        else:
            add_exprs[nid] = f"_ros({t!r}, {_lit(cfg)})"

    # ---- graph builder ----
    L.append("def make_graph(checkpointer=None):")
    L.append("    builder = StateGraph(State)")
    for n in live_nodes:
        nid = n["id"]
        L.append(f"    builder.add_node({_lit(nid)}, {add_exprs[nid]})")
    L.append("")
    # terminal end nodes
    for n in live_nodes:
        if n.get("type") == "end":
            L.append(f"    builder.add_edge({_lit(n['id'])}, END)")
    # routers
    for n in live_nodes:
        if n.get("type") == "router":
            cfg = cfg_of(n)
            L.append(f"    builder.add_conditional_edges({_lit(n['id'])}, make_router_path({_lit(cfg)}), (router_targets({_lit(cfg)}) or [END]))")
    # fanouts
    for n in live_nodes:
        if n.get("type") == "parallel_fanout":
            cfg = cfg_of(n)
            child = cfg.get("child_node")
            if child:
                L.append(f"    builder.add_conditional_edges({_lit(n['id'])}, make_fanout_path({_lit(cfg)}), [{_lit(child)}])")
    # explicit edges
    for e in edges:
        if not isinstance(e, dict):
            continue
        src, tgt = e.get("source"), e.get("target")
        if e.get("source_handle") == _SUBAGENT or src in routed:
            continue
        if src in child_ids or tgt in child_ids:
            continue
        if e.get("branches"):
            mapping = {str(k): v for k, v in e["branches"].items()}
            targets = sorted(set(mapping.values()))
            tgt_src = "[" + ", ".join([_lit(t) for t in targets] + ["END"]) + "]"
            L.append(f"    builder.add_conditional_edges({_lit(src)}, _branch({_lit(e.get('condition'))}, {_lit(mapping)}), {tgt_src})")
        else:
            tgt_expr = "END" if tgt in _END_TOKENS else _lit(tgt)
            L.append(f"    builder.add_edge({_lit(src)}, {tgt_expr})")
    L.append(f"    builder.add_edge(START, {_lit(entry)})")
    L.append("    return builder.compile(checkpointer=checkpointer)")
    L.append("")
    L.append("")
    L.append('if __name__ == "__main__":')
    L.append("    import asyncio")
    L.append("    from langgraph.checkpoint.memory import InMemorySaver")
    L.append("    g = make_graph(InMemorySaver())")
    L.append("    out = asyncio.run(g.ainvoke({'messages': [{'role': 'user', 'content': 'hello'}]},")
    L.append("                                {'configurable': {'thread_id': 'local-1'}}))")
    L.append("    print(out)")
    L.append("")
    return "\n".join(L)
