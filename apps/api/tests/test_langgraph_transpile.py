"""WS8: transpile executable JSON -> readable, standalone LangGraph graph.py (hybrid mode)."""

from __future__ import annotations

import importlib.util
import sys

from langgraph.checkpoint.memory import InMemorySaver

from ros.services.langgraph_transpile import transpile

_TRANSFORM_WF = {
    "id": "wf_t", "version": 1,
    "state": {
        "messages": {"type": "list[message]", "reducer": "add_messages"},
        "payload": {"type": "json", "reducer": "last"},
        "data": {"type": "json", "reducer": "last"},
        "acc": {"type": "list[str]", "reducer": "add"},
    },
    "entry_node": "start",
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {"id": "xf", "type": "transform", "config": {"expression": "payload", "output_key": "data"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [{"source": "start", "target": "xf"}, {"source": "xf", "target": "end"}],
}


def _load(src: str, tmp_path):
    """Import the generated source as a real module (how `langgraph dev` loads graph.py) — a bare
    exec() namespace would make State.__module__ == 'builtins' and break get_type_hints."""
    p = tmp_path / "gen_graph.py"
    p.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gen_graph_mod", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_graph_mod"] = mod  # normal import registers this; get_type_hints needs it
    spec.loader.exec_module(mod)
    return mod


def test_transpile_is_explicit_stategraph_not_black_box():
    src = transpile(_TRANSFORM_WF)
    compile(src, "graph.py", "exec")  # valid Python
    assert "StateGraph" in src and "builder.add_node" in src and "builder.add_edge" in src
    assert "compile_workflow" not in src  # not the opaque wrapper
    assert "jmespath.search" in src  # transform inlined as real code


async def test_transpiled_graph_actually_runs(tmp_path):
    # The whole point of hybrid mode: the emitted file is runnable LangGraph, no ROS compile step.
    mod = _load(transpile(_TRANSFORM_WF), tmp_path)
    graph = mod.make_graph(InMemorySaver())
    out = await graph.ainvoke({"payload": {"a": 1}}, {"configurable": {"thread_id": "t1"}})
    assert out["data"] == {"a": 1}


def test_transpile_agent_node_is_ros_backed():
    wf = {
        "id": "wf_a", "version": 1,
        "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
        "entry_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "agent_1", "type": "agent", "config": {"flavor": "agent", "model": "openai:gpt-4o-mini", "tools": []}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [{"source": "start", "target": "agent_1"}, {"source": "agent_1", "target": "end"}],
    }
    src = transpile(wf)
    compile(src, "graph.py", "exec")
    assert "_ros('agent'" in src and "def _ros(" in src  # delegates to the engine factory


def test_transpile_router_branches_fanout_valid():
    wf = {
        "id": "wf_r", "version": 1,
        "state": {
            "messages": {"type": "list[message]", "reducer": "add_messages"},
            "intent": {"type": "str", "reducer": "last"},
            "items": {"type": "list[json]", "reducer": "last"},
            "results": {"type": "list[str]", "reducer": "add"},
        },
        "entry_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "route", "type": "router", "config": {"expression": "intent", "cases": {"a": "fan"}, "default": "join1"}},
            {"id": "fan", "type": "parallel_fanout", "config": {"over": "items", "child_node": "work", "item_key": "item"}},
            {"id": "work", "type": "transform", "config": {"expression": "[item]", "output_key": "results"}},
            {"id": "join1", "type": "join", "config": {"reducer": "concat"}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "route"},
            {"source": "work", "target": "join1"},
            {"source": "join1", "target": "end", "condition": "intent", "branches": {"a": "end"}},
        ],
    }
    src = transpile(wf)
    compile(src, "graph.py", "exec")
    assert "make_router_path" in src and "make_fanout_path" in src and "_branch(" in src
    assert "add_conditional_edges" in src


def test_transpile_llm_structured_output_inlined():
    wf = {
        "id": "wf_l", "version": 1,
        "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
        "entry_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "llm1", "type": "llm", "config": {
                "model": "openai:gpt-4o-mini", "prompt": "Extract {{state.payload}}",
                "response_format": {"mode": "structured", "schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
            }},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [{"source": "start", "target": "llm1"}, {"source": "llm1", "target": "end"}],
    }
    src = transpile(wf)
    compile(src, "graph.py", "exec")
    assert "init_chat_model" in src and "with_structured_output" in src and "structured_response" in src
