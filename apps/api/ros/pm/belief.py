"""PM harness — provenance belief graph + deterministic revision engine.

The three organs of `docs/design/pm-harness.md`, implemented as PURE functions over a
JSON-native belief graph so the whole thing lives in the workflow's `beliefs` state field
(streams over SSE as a `belief_graph` frame, persists via the checkpointer) and is trivially
unit-testable:

  1. the belief store shape   — typed claims + typed provenance edges + a revision log
  2. the RevisionEngine       — bounded log-odds confidence + weakest-link cascade + lifecycle
  3. the Calibrator           — source-reliability priors, structural confidence, bands

Load-bearing design decision: the LLM proposes STRUCTURE (which claims exist, which edges of
which relation connect them); THIS module computes every NUMBER. There is no step where
"the model said 0.65" gates anything. That split makes revision deterministic,
order-independent, and testable — model unreliability is confined to structure proposal,
which a human can eyeball via the backward walk (`explain`).

Everything here is pure: no LLM, no IO, no wall-clock in the math (timestamps, when present,
are supplied by the caller and never feed the arithmetic), so shuffling the order in which
evidence arrives converges to the same graph.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Literal

# ── Vocabulary ───────────────────────────────────────────────────────────────────────────
ClaimType = Literal["evidence", "assumption", "hypothesis", "recommendation"]
Relation = Literal["supported_by", "contradicted_by", "derived_from", "assumes"]
Status = Literal["active", "promoted", "stale", "invalidated", "superseded"]
SourceType = Literal[
    "user", "document", "slack", "support", "analytics", "experiment", "agent_inference"
]

CLAIM_TYPES: tuple[str, ...] = ("evidence", "assumption", "hypothesis", "recommendation")
RELATIONS: tuple[str, ...] = ("supported_by", "contradicted_by", "derived_from", "assumes")

# ── Tunable constants (the calibration policy; §5 of the design doc) ──────────────────────
# Source-reliability priors: the base trust in a claim purely from WHERE it came from. These
# are config, not vibes; behavioural/experimental sources outrank qualitative ones, and the
# agent's own inference is the least trusted (must be corroborated). Overridable per profile.
SOURCE_PRIORS: dict[str, float] = {
    "experiment": 0.95,
    "analytics": 0.90,
    "document": 0.70,
    "support": 0.55,
    "user": 0.50,
    "slack": 0.45,
    "agent_inference": 0.30,
}
DEFAULT_PRIOR = 0.30

# Confidence is never certain: a single source can't drive a belief to 0 or 1.
C_MIN, C_MAX = 0.02, 0.98

# Assumption lifecycle thresholds (strength of the *evidence*, already calibrated).
PROMOTE_TAU = 0.80   # supporting evidence this strong promotes an assumption → treated ~ fact
KILL_TAU = 0.70      # contradicting evidence this strong invalidates an assumption

# A recommendation whose confidence falls below this (or that rests on an invalidated belief)
# is marked `stale` — surfaced for review, never silently deleted.
STALE_THRESHOLD = 0.34

# Confidence → band. Thirds; the boundary is what the UI and any decision gate key off, to
# avoid false precision.
BAND_HIGH, BAND_MED = 0.66, 0.34

_SIGN: dict[str, int] = {"supported_by": +1, "contradicted_by": -1}


# ── math helpers ─────────────────────────────────────────────────────────────────────────
def clamp(x: float, lo: float = C_MIN, hi: float = C_MAX) -> float:
    return lo if x < lo else hi if x > hi else x


def logit(p: float) -> float:
    p = clamp(p)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    # numerically stable
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def band(confidence: float) -> str:
    """Confidence → 'high' | 'medium' | 'low'. What the UI/decision boundary reads."""
    if confidence >= BAND_HIGH:
        return "high"
    if confidence >= BAND_MED:
        return "medium"
    return "low"


# ── identity / dedup ─────────────────────────────────────────────────────────────────────
_WS = re.compile(r"\s+")


def content_hash(statement: str) -> str:
    norm = _WS.sub(" ", (statement or "").strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def claim_id(claim_type: str, statement: str) -> str:
    """Deterministic id derived from (type, normalized statement). Same statement of the same
    type → same id → free exact-match dedup and reproducible tests."""
    return f"{claim_type[:3]}_{content_hash(statement)}"


def edge_id(src_id: str, dst_id: str, relation: str) -> str:
    return f"e_{content_hash(f'{src_id}|{relation}|{dst_id}')}"


# ── Calibrator (organ 3) ─────────────────────────────────────────────────────────────────
def _sample_factor(n: int) -> float:
    """Discount a measurement's reliability by sample size: small samples are shaky, large
    ones approach the source's ceiling. clamp keeps a single data point from vanishing and a
    huge sample from exceeding its source-type prior (analytics must still be able to
    outrank a big pile of support tickets — §5.1)."""
    if n <= 0:
        return 0.7
    return clamp(1.0 - 1.0 / math.sqrt(n), 0.6, 1.0)


def source_prior(source_type: str) -> float:
    return SOURCE_PRIORS.get(source_type, DEFAULT_PRIOR)


def initial_confidence(source_type: str, meta: dict | None = None) -> tuple[float, dict]:
    """Calibrated confidence for a *newly observed* claim, plus its `confidence_basis`
    (always attached so no number is ever shown without its provenance). Structural signal
    (a sample size `n`) scales confidence within the source's ceiling; otherwise the source
    prior stands alone. Raw model self-confidence is never used directly."""
    meta = meta or {}
    base = source_prior(source_type)
    n = meta.get("n")
    if isinstance(n, (int, float)) and n:
        conf = clamp(base * _sample_factor(int(n)))
        basis = {"method": "structural", "source_prior": base, "n": int(n)}
        if "p" in meta:
            basis["p"] = meta["p"]
        return conf, basis
    return clamp(base), {"method": "prior", "source_prior": base}


# ── belief store (organ 1) ───────────────────────────────────────────────────────────────
def empty_graph() -> dict[str, Any]:
    return {"claims": {}, "edges": [], "log": []}


def make_claim(
    claim_type: str,
    statement: str,
    source_type: str = "agent_inference",
    *,
    source_ref: str | None = None,
    meta: dict | None = None,
    created_at: str = "",
    confidence: float | None = None,
) -> dict[str, Any]:
    """Build a typed, provenance-carrying claim. `confidence` is normally left None so the
    Calibrator sets it; pass it only to seed a specific value in a test/demo. Timestamps are
    provenance only — they never feed the revision math."""
    if claim_type not in CLAIM_TYPES:
        raise ValueError(f"unknown claim type {claim_type!r}")
    if confidence is None:
        conf, basis = initial_confidence(source_type, meta)
    else:
        conf, basis = clamp(confidence), {"method": "explicit"}
    return {
        "id": claim_id(claim_type, statement),
        "type": claim_type,
        "statement": statement,
        "status": "active",
        "source_type": source_type,
        "source_ref": source_ref,
        "content_hash": content_hash(statement),
        "confidence": conf,
        "confidence_basis": basis,
        "meta": dict(meta or {}),
        "created_at": created_at,
    }


def make_edge(src_id: str, dst_id: str, relation: str, *, weight: float = 1.0) -> dict[str, Any]:
    if relation not in RELATIONS:
        raise ValueError(f"unknown relation {relation!r}")
    return {
        "id": edge_id(src_id, dst_id, relation),
        "src_id": src_id,
        "dst_id": dst_id,
        "relation": relation,
        "weight": float(weight),
    }


def add_claim(graph: dict, claim: dict) -> dict:
    """Insert a claim, deduped by id. Referential integrity is preserved by construction:
    claims are never hard-deleted (revision flips `status`), so edges can't be orphaned."""
    graph.setdefault("claims", {})
    if claim["id"] not in graph["claims"]:
        graph["claims"][claim["id"]] = claim
    return graph


def add_edge(graph: dict, edge: dict) -> dict:
    graph.setdefault("edges", [])
    if not any(e["id"] == edge["id"] for e in graph["edges"]):
        # Only wire edges between claims that exist in this graph (referential integrity).
        claims = graph.get("claims", {})
        if edge["src_id"] in claims and edge["dst_id"] in claims:
            graph["edges"].append(edge)
    return graph


# ── RevisionEngine (organ 2) ─────────────────────────────────────────────────────────────
def _edges_from(graph: dict, src_id: str, relations: tuple[str, ...]) -> list[dict]:
    return [
        e for e in graph.get("edges", [])
        if e["src_id"] == src_id and e["relation"] in relations
    ]


def _strength(graph: dict, claim_id_: str) -> float:
    c = graph["claims"].get(claim_id_)
    return c["confidence"] if c else C_MIN


def _revise_evidence_backed(graph: dict, claim: dict) -> float:
    """Log-odds accumulation for a claim (hypothesis/assumption) from its supporting and
    contradicting evidence. Additive log-odds is commutative → order-independent (§4.1):

        L = logit(prior)  +  Σ  sign(relation_i) · weight_i · logit(strength(evidence_i))
        confidence = clamp(sigmoid(L))

    Prior is 0.5 (logit 0), so a belief is driven purely by its evidence. Support pushes up,
    contradiction pushes down; both edges coexist (contradiction is preserved, not resolved
    by deletion), and the resulting confidence reflects the tension."""
    L = 0.0  # logit(0.5)
    for e in _edges_from(graph, claim["id"], ("supported_by", "contradicted_by")):
        ev = graph["claims"].get(e["dst_id"])
        if not ev or ev.get("status") == "invalidated":
            continue
        L += _SIGN[e["relation"]] * e.get("weight", 1.0) * logit(ev["confidence"])
    return clamp(sigmoid(L))


def _topo_order(graph: dict) -> list[str]:
    """Claim ids in dependency order: evidence → assumption → hypothesis → recommendation.
    The graph is shallow and typed, so a stable type-rank sort is a correct topological
    order (edges only ever point 'up' this rank)."""
    rank = {"evidence": 0, "assumption": 1, "hypothesis": 2, "recommendation": 3}
    return sorted(graph.get("claims", {}), key=lambda cid: rank.get(graph["claims"][cid]["type"], 9))


def revise(graph: dict) -> dict:
    """Recompute the whole graph deterministically from its current claims + edges, and run
    the lifecycle cascade. Pure recompute (not incremental) is what guarantees
    order-independence: however evidence arrived, the result is identical.

    Order: assumptions (lifecycle) → hypotheses (log-odds) → recommendations (weakest-link ×
    assumption penalty, with stale marking). Status transitions are appended to `log`
    (no timestamps, so the log stays deterministic)."""
    claims = graph.setdefault("claims", {})
    log: list[dict] = graph.setdefault("log", [])

    def _transition(claim: dict, new_status: str, reason: str) -> None:
        old = claim.get("status")
        if old != new_status:
            claim["status"] = new_status
            log.append({"claim_id": claim["id"], "from": old, "to": new_status, "reason": reason})

    for cid in _topo_order(graph):
        claim = claims[cid]
        ctype = claim["type"]

        if ctype == "evidence":
            continue  # evidence confidence is observed (calibrated at intake), not derived

        if ctype == "assumption":
            claim["confidence"] = _revise_evidence_backed(graph, claim)
            kill = [
                e for e in _edges_from(graph, cid, ("contradicted_by",))
                if _strength(graph, e["dst_id"]) >= KILL_TAU
            ]
            promote = [
                e for e in _edges_from(graph, cid, ("supported_by",))
                if _strength(graph, e["dst_id"]) >= PROMOTE_TAU
            ]
            if kill:
                _transition(claim, "invalidated", "contradicting evidence over kill threshold")
            elif promote and claim.get("status") != "invalidated":
                _transition(claim, "promoted", "supporting evidence over promote threshold")
            elif claim.get("status") in ("invalidated", "promoted"):
                # evidence changed such that neither condition holds any more → back to active
                _transition(claim, "active", "revised: lifecycle condition no longer met")

        elif ctype == "hypothesis":
            claim["confidence"] = _revise_evidence_backed(graph, claim)

        elif ctype == "recommendation":
            hyps = _edges_from(graph, cid, ("derived_from",))
            assumes = _edges_from(graph, cid, ("assumes",))
            # weakest link: a recommendation is only as strong as its shakiest hypothesis
            hyp_confs = [_strength(graph, e["dst_id"]) for e in hyps]
            base = min(hyp_confs) if hyp_confs else 0.5
            # assumption penalty: uncertain assumptions drag it down; an invalidated one tanks it
            # A *promoted* assumption is free; an *invalidated* one tanks the recommendation;
            # a merely-unresolved (active) assumption applies a MILD drag proportional to its
            # own uncertainty (a neutral 0.5 assumption must not by itself push a rec to stale).
            penalty = 1.0
            invalidated_dep = False
            for e in assumes:
                a = claims.get(e["dst_id"])
                if not a:
                    continue
                if a.get("status") == "invalidated":
                    penalty *= C_MIN
                    invalidated_dep = True
                elif a.get("status") != "promoted":
                    penalty *= 0.5 + 0.5 * a["confidence"]
            # an invalidated *hypothesis* also undermines the recommendation
            for e in hyps:
                h = claims.get(e["dst_id"])
                if h and h.get("status") == "invalidated":
                    invalidated_dep = True
            claim["confidence"] = clamp(base * penalty)
            if invalidated_dep or claim["confidence"] < STALE_THRESHOLD:
                _transition(claim, "stale", "confidence below threshold or rests on invalidated belief")
            elif claim.get("status") == "stale":
                _transition(claim, "active", "revised: recommendation confidence recovered")

    return graph


def apply_delta(
    graph: dict | None,
    claims: list[dict] | None = None,
    edges: list[dict] | None = None,
) -> dict:
    """Add newly-proposed claims/edges (deduped) then `revise()` the whole graph. This is the
    single entry point a node calls after the LLM proposes structure."""
    graph = graph or empty_graph()
    for c in claims or []:
        add_claim(graph, c)
    for e in edges or []:
        add_edge(graph, e)
    return revise(graph)


# ── backward walk / read model ───────────────────────────────────────────────────────────
def explain(graph: dict, claim_id_: str, _depth: int = 0, _seen: set | None = None) -> dict | None:
    """Reconstruct the provenance tree under a claim: 'why do we believe this?'. Answers a
    stakeholder's 'why is the agent recommending X?' by walking edges backwards to the
    grounding evidence — the governance/auditability surface."""
    _seen = _seen if _seen is not None else set()
    claim = graph.get("claims", {}).get(claim_id_)
    if not claim or _depth > 6 or claim_id_ in _seen:
        return None
    _seen = _seen | {claim_id_}
    node: dict[str, Any] = {
        "id": claim["id"],
        "type": claim["type"],
        "statement": claim["statement"],
        "status": claim.get("status", "active"),
        "confidence": round(claim["confidence"], 3),
        "band": band(claim["confidence"]),
        "source_type": claim.get("source_type"),
    }
    for rel in RELATIONS:
        children = []
        for e in _edges_from(graph, claim_id_, (rel,)):
            sub = explain(graph, e["dst_id"], _depth + 1, _seen)
            if sub:
                children.append(sub)
        if children:
            node[rel] = children
    return node


def summarize(graph: dict) -> dict:
    """Compact, glanceable belief state for a stream frame / node output."""
    claims = list(graph.get("claims", {}).values())
    by_type: dict[str, int] = {}
    for c in claims:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    recs = [
        {"statement": c["statement"], "confidence": round(c["confidence"], 3),
         "band": band(c["confidence"]), "status": c.get("status")}
        for c in claims if c["type"] == "recommendation"
    ]
    return {
        "counts": by_type,
        "edges": len(graph.get("edges", [])),
        "recommendations": recs,
        "revisions": len(graph.get("log", [])),
    }
