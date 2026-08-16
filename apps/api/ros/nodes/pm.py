"""`pm_reason` node — the PM harness reasoning loop as a first-class workflow node.

One parametric node type whose `step` config selects which stage of the adaptive loop it is
(see docs/design/pm-harness.md). The reasoning STRUCTURE (claims + provenance edges) lives in
the `beliefs` state field; the deterministic RevisionEngine (ros/pm/belief.py) computes every
confidence in the `update_beliefs` step. Each firing pushes a `belief_graph` custom SSE frame
so the console can render the belief graph revising live.

The loop wires up on the canvas from existing primitives:

    start → understand_context → prioritize_unknowns → gather_evidence → update_beliefs
          → decide_progress → router(pm_route){produce|ask}
          produce_work → end ;  ask_user(human_input) → loop → router(state['_loop']){gather|produce}

Every step is offline-safe and deterministic when seeded (so a demo runs on the `fake:` model
with no keys); the `model`/tool hooks are the extension points where the LLM proposes structure
and tools resolve unknowns in a real deployment.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ros.engine.context import CompileContext
from ros.engine.registry import NodeSpec, Port, register
from ros.pm import belief as B

log = logging.getLogger("ros.pm")

STEPS = (
    "understand_context",
    "prioritize_unknowns",
    "gather_evidence",
    "update_beliefs",
    "decide_progress",
    "produce_work",
)


# ── helpers ────────────────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_of(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    return content if isinstance(content, str) else str(content or "")


def _role_of(message: Any) -> str | None:
    return message.get("role") if isinstance(message, dict) else getattr(message, "type", None)


def _last_human(state: dict) -> str:
    for m in reversed(state.get("messages") or []):
        if _role_of(m) in ("human", "user") and _text_of(m).strip():
            return _text_of(m).strip()
    return ""


def _seed(graph: dict, seed_claims: list[dict] | None, seed_edges: list[dict] | None) -> dict:
    """Materialize author/model-proposed structure into the graph, then revise. Claims are
    calibrated at intake; edges reference claims by their statement text (author-friendly) and
    are resolved to content-hash ids here."""
    created_at = _now()
    new_claims: list[dict] = []
    stmt_to_id: dict[str, str] = {}
    for cid, c in graph.get("claims", {}).items():
        stmt_to_id[c["statement"].strip().lower()] = cid
    for sc in seed_claims or []:
        claim = B.make_claim(
            sc["type"], sc["statement"], sc.get("source_type", "agent_inference"),
            source_ref=sc.get("source_ref"), meta=sc.get("meta"),
            created_at=created_at, confidence=sc.get("confidence"),
        )
        new_claims.append(claim)
        stmt_to_id[claim["statement"].strip().lower()] = claim["id"]
    new_edges: list[dict] = []
    for se in seed_edges or []:
        src = stmt_to_id.get(str(se.get("src", "")).strip().lower())
        dst = stmt_to_id.get(str(se.get("dst", "")).strip().lower())
        if src and dst:
            new_edges.append(B.make_edge(src, dst, se["relation"], weight=float(se.get("weight", 1.0))))
        else:
            log.warning("pm_reason seed_edge references unknown claim: %r", se)
    return B.apply_delta(graph, new_claims, new_edges)


def _emit(step: str, graph: dict) -> None:
    """Push the live belief graph to the run stream as a `belief_graph` custom frame (same
    mechanism as the emit_event node). No-op when there's no active stream writer (ainvoke)."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()({
            "channel": "belief_graph",
            "payload": {"step": step, "graph": graph, "summary": B.summarize(graph)},
        })
    except Exception:  # noqa: BLE001 - no active stream writer
        pass


def _has_evidence(graph: dict) -> bool:
    return any(c["type"] == "evidence" for c in graph.get("claims", {}).values())


def _ensure_recommendation(graph: dict) -> dict:
    """If the graph carries no recommendation, synthesize one from the strongest active
    hypothesis so produce_work always has something to render (derived_from that hypothesis,
    so the backward walk stays intact)."""
    claims = graph.get("claims", {})
    if any(c["type"] == "recommendation" for c in claims.values()):
        return graph
    hyps = [c for c in claims.values() if c["type"] == "hypothesis" and c.get("status") != "invalidated"]
    if not hyps:
        return graph
    top = max(hyps, key=lambda c: c["confidence"])
    rec = B.make_claim("recommendation", f"Act on: {top['statement']}", "agent_inference", created_at=_now())
    return B.apply_delta(graph, [rec], [B.make_edge(rec["id"], top["id"], "derived_from")])


def _render_answer(graph: dict, state: dict) -> str:
    """Deterministic, provenance-faithful recommendation memo from the belief graph:
    recommendation + confidence band + the facts it rests on + contradicting evidence +
    unresolved assumptions/questions, explicitly marked provisional. (A configured LLM would
    write this prose in a real deployment; the template keeps the demo deterministic.)"""
    claims = graph.get("claims", {})
    recs = sorted(
        [c for c in claims.values() if c["type"] == "recommendation"],
        key=lambda c: c["confidence"], reverse=True,
    )
    lines: list[str] = []
    if recs:
        top = recs[0]
        note = " (STALE — rests on a weakened/invalidated belief)" if top.get("status") == "stale" else ""
        lines.append(f"**Recommendation** ({B.band(top['confidence'])} confidence{note}): {top['statement']}")
        tree = B.explain(graph, top["id"]) or {}
        for h in tree.get("derived_from", []):
            lines.append(f"\n_Because_: {h['statement']} ({h['band']} confidence)")
            for ev in h.get("supported_by", []):
                lines.append(f"  • supported by: {ev['statement']} [{ev['source_type']}]")
            for ev in h.get("contradicted_by", []):
                lines.append(f"  ✗ contradicted by: {ev['statement']} [{ev['source_type']}]")
    else:
        lines.append("No recommendation yet — insufficient grounded belief.")

    open_qs = [u.get("question") for u in (state.get("critical_unknowns") or state.get("unknowns") or [])
               if u.get("question")]
    if open_qs:
        lines.append("\n**Open questions:**")
        lines.extend(f"  - {q}" for q in open_qs)

    assumptions = [c for c in claims.values() if c["type"] == "assumption" and c.get("status") == "active"]
    if assumptions:
        lines.append("\n**Unresolved assumptions:**")
        lines.extend(f"  - {a['statement']} ({B.band(a['confidence'])})" for a in assumptions)

    lines.append("\n_Provisional — revise as new evidence/tools become available._")
    return "\n".join(lines)


# ── the node ─────────────────────────────────────────────────────────────────────────────
def pm_reason_factory(config: dict, ctx: CompileContext):
    step = config.get("step")
    if step not in STEPS:
        raise ValueError(f"pm_reason: step must be one of {STEPS}, got {step!r}")

    async def _node(state: dict) -> dict:
        from langchain_core.messages import AIMessage

        graph = dict(state.get("beliefs") or B.empty_graph())
        out: dict[str, Any] = {}

        if step == "understand_context":
            out["objective"] = config.get("objective") or _last_human(state) or state.get("objective") or ""
            if config.get("unknowns"):
                out["unknowns"] = config["unknowns"]
            graph = _seed(graph, config.get("seed_claims"), config.get("seed_edges"))
            out["beliefs"] = graph

        elif step == "prioritize_unknowns":
            # Keep only decision-sensitive unknowns: blocking, or high decision_impact.
            unknowns = state.get("unknowns") or config.get("unknowns") or []
            out["critical_unknowns"] = [
                u for u in unknowns if u.get("blocking") or u.get("decision_impact") == "high"
            ]

        elif step == "gather_evidence":
            # Resolve unknowns from seeded evidence (and, in a real deployment, from tools in
            # ctx.tool_registry — the capability-upgrade hook). Deterministic when seeded.
            graph = _seed(graph, config.get("seed_claims"), config.get("seed_edges"))
            out["beliefs"] = graph

        elif step == "update_beliefs":
            # The crown jewel: pure deterministic recompute + lifecycle cascade.
            graph = B.revise(graph)
            out["beliefs"] = graph

        elif step == "decide_progress":
            crit = state.get("critical_unknowns") or []
            must_ask = any(u.get("blocking") and u.get("ask_human") for u in crit)
            # Default to progress: only pause for a human when a blocking unknown remains AND we
            # have no evidence to proceed on.
            can = _has_evidence(graph) or not must_ask
            out["can_make_progress"] = bool(can)
            # `pm_route` (no leading underscore) so a `router` can read it as a bare name;
            # the RestrictedPython expression sandbox rejects underscore-prefixed names.
            out["pm_route"] = "produce" if can else "ask"

        elif step == "produce_work":
            graph = _ensure_recommendation(graph)
            answer = _render_answer(graph, state)
            out["answer"] = answer
            out["messages"] = [AIMessage(content=answer)]
            out["beliefs"] = graph

        _emit(step, graph)
        return out

    return _node


_STEP_LABEL = {
    "understand_context": "understand",
    "prioritize_unknowns": "prioritize",
    "gather_evidence": "gather",
    "update_beliefs": "revise beliefs",
    "decide_progress": "decide",
    "produce_work": "produce",
}

register(NodeSpec(
    type="pm_reason",
    schema_id="ros/nodes/pm_reason",
    input_ports=[Port(id="in", io_type="any", direction="in")],
    output_ports=[Port(id="out", io_type="any", direction="out")],
    factory=pm_reason_factory,
    category="reasoning",
    label="PM Reason",
    description="One stage of the PM harness belief-revision loop.",
    summarize=lambda c: [_STEP_LABEL.get(c.get("step", ""), c.get("step", "?")), "belief graph"],
))
