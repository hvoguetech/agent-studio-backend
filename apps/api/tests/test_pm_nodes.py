"""`pm_reason` node — compiles + runs through the real engine, updates the belief graph,
and streams live `belief_graph` frames. Mirrors tests/test_advanced_nodes.py.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from ros.engine.compiler import compile_workflow
from ros.pm import belief as B
from ros.services.runtime import make_runtime_ctx

_HYP = "Users get stuck at Wi-Fi configuration"

_PM_WF = {
    "id": "pm", "version": 1,
    "state": {
        "messages": {"type": "list[message]", "reducer": "add_messages"},
        "objective": {"type": "str", "reducer": "last"},
        "beliefs": {"type": "json", "reducer": "last"},
        "unknowns": {"type": "list[json]", "reducer": "last"},
        "critical_unknowns": {"type": "list[json]", "reducer": "last"},
        "pm_route": {"type": "str", "reducer": "last"},
        "can_make_progress": {"type": "bool", "reducer": "last"},
        "answer": {"type": "str", "reducer": "last"},
        "_ask_ack": {"type": "str", "reducer": "last"},
    },
    "entry_node": "start",
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {"id": "understand", "type": "pm_reason", "config": {
            "step": "understand_context",
            "objective": "Improve onboarding completion",
            "unknowns": [{"question": "Which step causes most friction?", "decision_impact": "high", "blocking": False}],
            "seed_claims": [
                {"type": "hypothesis", "statement": _HYP},
                {"type": "assumption", "statement": "Reducing support burden is worth the investment"},
            ],
        }},
        {"id": "prioritize", "type": "pm_reason", "config": {"step": "prioritize_unknowns"}},
        {"id": "gather", "type": "pm_reason", "config": {
            "step": "gather_evidence",
            "seed_claims": [
                {"type": "evidence", "statement": "31% of installation tickets mention Wi-Fi", "source_type": "support", "meta": {"n": 837}},
                {"type": "evidence", "statement": "Analytics shows only 3% drop-off at Wi-Fi step", "source_type": "analytics", "meta": {"n": 5000}},
            ],
            "seed_edges": [
                {"src": _HYP, "dst": "31% of installation tickets mention Wi-Fi", "relation": "supported_by"},
                {"src": _HYP, "dst": "Analytics shows only 3% drop-off at Wi-Fi step", "relation": "contradicted_by"},
            ],
        }},
        {"id": "update", "type": "pm_reason", "config": {"step": "update_beliefs"}},
        {"id": "decide", "type": "pm_reason", "config": {"step": "decide_progress"}},
        {"id": "route", "type": "router", "config": {"expression": "pm_route", "cases": {"produce": "produce", "ask": "ask"}, "default": "produce"}},
        {"id": "produce", "type": "pm_reason", "config": {"step": "produce_work"}},
        {"id": "ask", "type": "human_input", "config": {"prompt": "Which step?", "output_key": "_ask_ack"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [
        {"source": "start", "target": "understand"},
        {"source": "understand", "target": "prioritize"},
        {"source": "prioritize", "target": "gather"},
        {"source": "gather", "target": "update"},
        {"source": "update", "target": "decide"},
        {"source": "decide", "target": "route"},
        {"source": "produce", "target": "end"},
        {"source": "ask", "target": "end"},
    ],
}


def _ctx():
    ctx = make_runtime_ctx("t_pm", "p_pm")
    ctx.checkpointer = InMemorySaver()
    return ctx


async def test_pm_workflow_compiles_and_runs():
    graph = compile_workflow(_PM_WF, _ctx())
    out = await graph.ainvoke({}, {"configurable": {"thread_id": "pm1"}})

    assert out["objective"] == "Improve onboarding completion"
    assert out["can_make_progress"] is True  # evidence present → default to progress
    assert out["pm_route"] == "produce"

    g = out["beliefs"]
    kinds = {c["type"] for c in g["claims"].values()}
    assert {"hypothesis", "evidence", "recommendation"} <= kinds

    # analytics contradiction weakened the hypothesis into the low band
    hyp = g["claims"][B.claim_id("hypothesis", _HYP)]
    assert B.band(hyp["confidence"]) == "low"

    assert "Recommendation" in out["answer"]
    # both provenance edges survive on the hypothesis (contradiction preserved)
    rels = {e["relation"] for e in g["edges"] if e["src_id"] == hyp["id"]}
    assert rels == {"supported_by", "contradicted_by"}


# Mirrors the console's `pmReasoningWorkflow()` generator (lib/graph.ts) — keep in sync.
# ACYCLIC: the validator only permits cycles through `allows_cycle` nodes, so there is no
# loop-back edge; iteration happens by re-running the workflow (design-doc: loop via re-invoke).
_FULL_TEMPLATE = {
    "id": "pm_full", "version": 1,
    "state": {
        "messages": {"type": "list[message]", "reducer": "add_messages"},
        "objective": {"type": "str", "reducer": "last"},
        "beliefs": {"type": "json", "reducer": "last"},
        "unknowns": {"type": "list[json]", "reducer": "last"},
        "critical_unknowns": {"type": "list[json]", "reducer": "last"},
        "pm_route": {"type": "str", "reducer": "last"},
        "can_make_progress": {"type": "bool", "reducer": "last"},
        "answer": {"type": "str", "reducer": "last"},
        "_ask_ack": {"type": "str", "reducer": "last"},
    },
    "entry_node": "start",
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {"id": "understand", "type": "pm_reason", "config": {
            "step": "understand_context", "objective": "Improve onboarding completion",
            "unknowns": [{"question": "Which step causes most friction?", "decision_impact": "high", "blocking": False}],
            "seed_claims": [{"type": "hypothesis", "statement": _HYP},
                            {"type": "assumption", "statement": "Reducing support burden is worth the investment"}],
        }},
        {"id": "prioritize", "type": "pm_reason", "config": {"step": "prioritize_unknowns"}},
        {"id": "gather", "type": "pm_reason", "config": {
            "step": "gather_evidence",
            "seed_claims": [{"type": "evidence", "statement": "31% of installation tickets mention Wi-Fi", "source_type": "support", "meta": {"n": 837}},
                            {"type": "evidence", "statement": "Analytics shows only 3% drop-off at Wi-Fi step", "source_type": "analytics", "meta": {"n": 5000}}],
            "seed_edges": [{"src": _HYP, "dst": "31% of installation tickets mention Wi-Fi", "relation": "supported_by"},
                           {"src": _HYP, "dst": "Analytics shows only 3% drop-off at Wi-Fi step", "relation": "contradicted_by"}],
        }},
        {"id": "update", "type": "pm_reason", "config": {"step": "update_beliefs"}},
        {"id": "decide", "type": "pm_reason", "config": {"step": "decide_progress"}},
        {"id": "route", "type": "router", "config": {"expression": "pm_route", "cases": {"produce": "produce", "ask": "ask"}, "default": "produce"}},
        {"id": "produce", "type": "pm_reason", "config": {"step": "produce_work"}},
        {"id": "ask", "type": "human_input", "config": {"prompt": "Which onboarding step should we prioritize?", "output_key": "_ask_ack"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [
        {"source": "start", "target": "understand"},
        {"source": "understand", "target": "prioritize"},
        {"source": "prioritize", "target": "gather"},
        {"source": "gather", "target": "update"},
        {"source": "update", "target": "decide"},
        {"source": "decide", "target": "route"},
        {"source": "ask", "target": "produce"},
        {"source": "produce", "target": "end"},
    ],
}


def test_pm_full_template_validates_clean():
    """The exact graph the console's 'New PM workflow' template generates must pass the
    validator with NO errors and NO warnings — this is the save/publish gate the UI enforces."""
    from ros.services.validation import validate_workflow

    res = validate_workflow(_FULL_TEMPLATE)
    assert res.valid, res.errors
    # decide_progress is registered as writing pm_route, so the router doesn't false-warn.
    assert not res.warnings, res.warnings


async def test_pm_full_template_compiles_and_runs():
    """The template must also compile and run to a recommendation on the happy path
    (evidence present → route to produce)."""
    graph = compile_workflow(_FULL_TEMPLATE, _ctx())
    out = await graph.ainvoke({}, {"configurable": {"thread_id": "pmfull"}})
    assert out["pm_route"] == "produce"
    assert "Recommendation" in out["answer"]
    assert any(c["type"] == "recommendation" for c in out["beliefs"]["claims"].values())


async def test_pm_emits_live_belief_graph_frames():
    graph = compile_workflow(_PM_WF, _ctx())
    steps_seen: list[str] = []
    async for _ns, mode, chunk in graph.astream(
        {}, {"configurable": {"thread_id": "pm2"}}, stream_mode=["custom"], subgraphs=True
    ):
        if isinstance(chunk, dict) and chunk.get("channel") == "belief_graph":
            steps_seen.append(chunk["payload"]["step"])

    # every reasoning stage pushed a live belief-graph frame
    assert "update_beliefs" in steps_seen
    assert "produce_work" in steps_seen
    assert len(steps_seen) >= 5
