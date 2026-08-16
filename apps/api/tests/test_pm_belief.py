"""Pure belief-revision core (ros/pm/belief.py) — the three organs.

These tests pin the properties the design doc promises: calibrated intake confidence,
order-independent log-odds revision, preserved contradiction, weakest-link cascade,
assumption lifecycle, and the Wi-Fi backward-walk golden. No LLM, no IO.
"""

from __future__ import annotations

import math

from ros.pm import belief as B


# ── Calibrator (organ 3) ──────────────────────────────────────────────────────────────────
def test_initial_confidence_structural_from_sample_size():
    conf, basis = B.initial_confidence("support", {"n": 837, "p": 0.31})
    assert basis["method"] == "structural"
    assert basis["n"] == 837 and basis["p"] == 0.31
    expected = B.SOURCE_PRIORS["support"] * B._sample_factor(837)
    assert math.isclose(conf, expected, rel_tol=1e-9)


def test_initial_confidence_prior_when_no_structure():
    conf, basis = B.initial_confidence("user")
    assert basis["method"] == "prior"
    assert math.isclose(conf, B.SOURCE_PRIORS["user"])


def test_source_prior_ordering_behavioural_outranks_qualitative():
    assert B.source_prior("experiment") > B.source_prior("analytics") > B.source_prior("support")
    assert B.source_prior("support") > B.source_prior("agent_inference")


def test_bands():
    assert B.band(0.9) == "high"
    assert B.band(0.5) == "medium"
    assert B.band(0.1) == "low"


# ── RevisionEngine (organ 2) ───────────────────────────────────────────────────────────────
def _hyp_with_evidence():
    ev_a = B.make_claim("evidence", "31% of 837 tickets mention Wi-Fi", "support", meta={"n": 837})
    ev_b = B.make_claim("evidence", "7 of 10 interviewed installers complained", "user", meta={"n": 10})
    hyp = B.make_claim("hypothesis", "Wi-Fi configuration causes significant friction")
    edges = [
        B.make_edge(hyp["id"], ev_a["id"], "supported_by"),
        B.make_edge(hyp["id"], ev_b["id"], "supported_by"),
    ]
    return hyp, ev_a, ev_b, edges


def test_log_odds_is_order_independent():
    hyp, ev_a, ev_b, edges = _hyp_with_evidence()
    ev_c = B.make_claim("evidence", "Only 3% funnel drop-off at Wi-Fi step", "analytics", meta={"n": 5000})
    contra = B.make_edge(hyp["id"], ev_c["id"], "contradicted_by")

    claims = [hyp, ev_a, ev_b, ev_c]
    all_edges = edges + [contra]

    # apply everything in three different arrival orders → identical hypothesis confidence
    g1 = B.apply_delta(B.empty_graph(), claims, all_edges)
    g2 = B.apply_delta(B.empty_graph(), list(reversed(claims)), list(reversed(all_edges)))
    g3 = B.apply_delta(B.empty_graph(), [ev_c, hyp, ev_a, ev_b], [contra] + edges)

    c1 = g1["claims"][hyp["id"]]["confidence"]
    assert math.isclose(c1, g2["claims"][hyp["id"]]["confidence"], rel_tol=1e-12)
    assert math.isclose(c1, g3["claims"][hyp["id"]]["confidence"], rel_tol=1e-12)


def test_contradiction_lowers_confidence_and_is_preserved():
    hyp, ev_a, ev_b, edges = _hyp_with_evidence()
    g = B.apply_delta(B.empty_graph(), [hyp, ev_a, ev_b], edges)
    before = g["claims"][hyp["id"]]["confidence"]

    ev_c = B.make_claim("evidence", "Only 3% funnel drop-off at Wi-Fi step", "analytics", meta={"n": 5000})
    g = B.apply_delta(g, [ev_c], [B.make_edge(hyp["id"], ev_c["id"], "contradicted_by")])
    after = g["claims"][hyp["id"]]["confidence"]

    assert after < before  # contradiction strictly weakens
    # both the supporting and contradicting edges coexist — contradiction is not resolved by deletion
    rels = {e["relation"] for e in g["edges"] if e["src_id"] == hyp["id"]}
    assert rels == {"supported_by", "contradicted_by"}


def test_unsupported_hypothesis_sits_at_prior():
    hyp = B.make_claim("hypothesis", "some unbacked guess")
    g = B.apply_delta(B.empty_graph(), [hyp], [])
    assert math.isclose(g["claims"][hyp["id"]]["confidence"], 0.5, abs_tol=1e-9)


def test_weakest_link_recommendation():
    strong = B.make_claim("hypothesis", "strong hypothesis")
    weak = B.make_claim("hypothesis", "weak hypothesis")
    ev_s = B.make_claim("evidence", "solid experiment result", "experiment", meta={"n": 4000})
    ev_w = B.make_claim("evidence", "one offhand slack remark", "slack", meta={"n": 1})
    rec = B.make_claim("recommendation", "do the thing")
    claims = [strong, weak, ev_s, ev_w, rec]
    edges = [
        B.make_edge(strong["id"], ev_s["id"], "supported_by"),
        B.make_edge(weak["id"], ev_w["id"], "supported_by"),
        B.make_edge(rec["id"], strong["id"], "derived_from"),
        B.make_edge(rec["id"], weak["id"], "derived_from"),
    ]
    g = B.apply_delta(B.empty_graph(), claims, edges)
    weakest = min(g["claims"][strong["id"]]["confidence"], g["claims"][weak["id"]]["confidence"])
    # recommendation is capped by its shakiest supporting hypothesis (no assumptions here)
    assert g["claims"][rec["id"]]["confidence"] <= weakest + 1e-9


def test_assumption_invalidation_cascades_recommendation_to_stale():
    assume = B.make_claim("assumption", "reducing support burden is worth the investment")
    hyp = B.make_claim("hypothesis", "Wi-Fi is the main pain")
    ev = B.make_claim("evidence", "strong experiment shows Wi-Fi is fine", "experiment", meta={"n": 3000})
    rec = B.make_claim("recommendation", "redesign the Wi-Fi flow")
    claims = [assume, hyp, ev, rec]
    edges = [
        B.make_edge(rec["id"], hyp["id"], "derived_from"),
        B.make_edge(rec["id"], assume["id"], "assumes"),
    ]
    g = B.apply_delta(B.empty_graph(), claims, edges)
    assert g["claims"][rec["id"]]["status"] != "stale"  # nothing contradicts yet

    # a strong contradiction of the assumption invalidates it and cascades to the rec
    g = B.apply_delta(g, [ev], [B.make_edge(assume["id"], ev["id"], "contradicted_by")])
    assert g["claims"][assume["id"]]["status"] == "invalidated"
    assert g["claims"][rec["id"]]["status"] == "stale"


def test_assumption_promoted_by_strong_support():
    assume = B.make_claim("assumption", "step 3 causes most failures")
    ev = B.make_claim("evidence", "experiment confirms step 3 failures", "experiment", meta={"n": 3000})
    g = B.apply_delta(B.empty_graph(), [assume, ev], [B.make_edge(assume["id"], ev["id"], "supported_by")])
    assert g["claims"][assume["id"]]["status"] == "promoted"


def test_dedup_same_statement_same_id():
    c1 = B.make_claim("evidence", "Users struggle at Wi-Fi", "user")
    c2 = B.make_claim("evidence", "users   struggle at   wi-fi", "support")  # whitespace/case variant
    assert c1["id"] == c2["id"]
    g = B.apply_delta(B.empty_graph(), [c1, c2], [])
    assert len(g["claims"]) == 1


# ── Wi-Fi golden (design doc §, the backward walk) ──────────────────────────────────────────
def test_wifi_golden_backward_walk():
    # T0: user hypothesis, weakly held
    hyp = B.make_claim("hypothesis", "Users get stuck at Wi-Fi configuration")
    g = B.apply_delta(B.empty_graph(), [hyp], [])

    # T1/T2: qualitative support arrives
    ev_sup = B.make_claim("evidence", "31% of installation tickets mention Wi-Fi", "support", meta={"n": 837})
    ev_int = B.make_claim("evidence", "7/10 interviewed installers complained about Wi-Fi", "user", meta={"n": 10})
    g = B.apply_delta(g, [ev_sup, ev_int], [
        B.make_edge(hyp["id"], ev_sup["id"], "supported_by"),
        B.make_edge(hyp["id"], ev_int["id"], "supported_by"),
    ])
    supported = g["claims"][hyp["id"]]["confidence"]

    # T3: analytics contradicts
    ev_ana = B.make_claim("evidence", "Analytics shows only 3% drop-off at Wi-Fi step", "analytics", meta={"n": 5000})
    g = B.apply_delta(g, [ev_ana], [B.make_edge(hyp["id"], ev_ana["id"], "contradicted_by")])
    weakened = g["claims"][hyp["id"]]["confidence"]

    assert weakened < supported                       # behavioural data weakens the hypothesis
    assert B.band(weakened) != "high"                 # no longer strongly held

    tree = B.explain(g, hyp["id"])
    assert tree is not None
    assert "supported_by" in tree and "contradicted_by" in tree
    assert len(tree["supported_by"]) == 2 and len(tree["contradicted_by"]) == 1
