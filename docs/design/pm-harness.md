# Design — PM harness: provenance belief graph + adaptive reasoning loop

**Status:** Design (critique-to-spec)
**Goal:** a product-manager agent that is useful from a cold start (no metrics, no tools) and gets
**monotonically** more capable as evidence and tools arrive — while maintaining an **inspectable,
revisable model of what it believes, why, what it doesn't know, and what would change its mind.**

**Provenance:** distilled from two source notes (the "agentic loop" manifesto and the "LangGraph +
provenance" sketch). This doc keeps what those got right and specifies the **three organs they left
as `return {...}` stubs**. Those three are the whole ballgame:

1. **A persistent belief store with integrity** — the typed provenance graph as a real store, not
   `TypedDict`s in LangGraph state (§3).
2. **A belief-revision & confidence-propagation policy** — deterministic update semantics over that
   graph, including cascade-invalidation (§4).
3. **Calibrated uncertainty** — where confidence numbers come from and why they can be trusted (§5).

Everything else in the source sketch (the four buckets, the near-linear node flow, the tool
registry) is scaffolding around these three.

## 0. What the source sketch got right (keep)

- **Typed claims with provenance** (`Evidence / Assumption / Hypothesis / Recommendation`) and a
  walkable `Evidence → Hypothesis → Recommendation` graph. This is the real idea.
- **`decision_impact` vs `blocking`** — important ≠ blocking; default to progress, escalate only
  what actually gates the next action.
- **Tools as capability upgrades**, selected by `unknown → available capability → best resolver`,
  not hardcoded per-workflow.
- **Same architecture, rising capability** as context/permissions grow (the T0→T3 enrichment).

## 0.1 What it got wrong (fix here)

- **The compiled graph does not loop.** In the sketch, `produce_work → END`, `ask_user` has no out
  edge, and nothing re-enters `gather_evidence` when a tool arrives. The advertised "loop" only
  exists by re-invoking the graph with carried-forward state. We make the loop explicit via
  checkpoint + `interrupt` (§6).
- **State ≠ store.** The valuable asset must survive across turns, sessions, and tool-arrivals. That
  is a database, not ephemeral graph state. Mirrors our artifact-storage rule: **the belief graph
  lives in the store; only a working-set reference goes in checkpoint state.**
- **Confidence is hand-waved.** `0.65`, `0.5`, `0.95` appear with no source. Uncalibrated LLM
  self-confidence cannot gate decisions. §5 replaces it.
- **No revision mechanism.** "Revise, don't restart" and "hypothesis therefore weakened" are narrated,
  never computed. §4 makes it a deterministic engine.

## 1. Goals / non-goals

**Goals**
- Belief state is **first-class, typed, provenance-carrying, and persistent** — every claim answers
  "why do we believe this?" and "what would change it?".
- Belief revision is **deterministic and testable** — the LLM proposes structure; a pure policy
  engine computes confidence. No LLM in the propagation loop.
- Confidence is **calibrated or conservative** — never false precision, always provenance-conditioned.
- Runs as a **governed subject** on `ros`: an `AgentProfile`, tenant-isolated by RLS, gated by
  `authz` permissions, tools materialized behind the tool gate.
- **Cold-start useful; monotonically improving** as `runtime_env` tools/permissions expand.

**Non-goals (deferred)**
- Autonomous action on the world (the PM agent **recommends**; it does not ship). Writes to Jira/etc.
  are a later, separately-gated capability.
- Cross-project belief sharing / org-wide knowledge base. Belief graphs are scoped per reasoning
  session for now (§3.2).
- Learned source-reliability priors — v1 uses configured priors; learning is §5.4 future work.
- Multi-agent debate over a shared graph.

## 2. Seam

```
PMReasoner (service)                     the loop: propose → resolve → revise → decide   (§6)
   ├── BeliefStore (service)             typed claims + provenance edges + revision log   (§3)
   │      └── backend: Postgres+RLS      normalized tables; tenant/project/session scoped
   ├── RevisionEngine (pure)             deterministic confidence propagation + cascade    (§4)
   │      └── no LLM, no IO — pure fn(graph, new_claims) -> graph'
   ├── Calibrator (pure + config)        source priors, structural confidence, calibration (§5)
   ├── CapabilityResolver                unknown -> available tool -> best resolver         (§6.2)
   │      └── ros tool registry + authz gate + AgentProfile.runtime_env
   └── LangGraph (thin orchestration)    checkpointer (durable) + interrupt (ask_user)      (§6)
```

Selection/scoping mirrors existing `ros` seams (execution, artifact-storage): backend-agnostic
service on top, one pluggable store underneath, LLM confined to structure-proposal at the edges.

---

## 3. Organ 1 — persistent belief store with integrity

### 3.1 Data model (normalized, first-class, provenance-carrying)

Four node types share a base; edges are a first-class table (the reasoning graph).

```python
# ros/pm/models.py  (SQLModel/SQLAlchemy — Postgres, RLS-scoped like every ros entity)

ClaimType   = Literal["evidence", "assumption", "hypothesis", "recommendation"]
SourceType  = Literal["user", "document", "slack", "support", "analytics",
                      "experiment", "agent_inference"]
ClaimStatus = Literal["active", "promoted", "stale", "invalidated", "superseded"]

class Claim:              # one table, discriminated by `type`
    id: str              # ULID; stable, referenced by edges
    tenant_id: str       # RLS scope (mandatory floor)
    project_id: str
    session_id: str      # the reasoning session / objective this belief belongs to (§3.2)
    agent_id: str        # AgentProfile that owns the belief

    type: ClaimType
    statement: str
    status: ClaimStatus = "active"

    # provenance
    source_type: SourceType
    source_ref: str | None     # support_query_837, doc://..., run_id, message_id
    content_hash: str          # sha256(normalized statement) — dedup key (§3.3)

    # confidence (see §5 — never a raw LLM float)
    confidence: float          # 0..1, computed by Calibrator/RevisionEngine
    confidence_basis: dict     # {"method": "structural|prior|calibrated", inputs...}

    created_at: str
    superseded_by: str | None  # append-only history: never hard-delete (§3.4)

class BeliefEdge:              # the graph; directed, typed
    id: str
    tenant_id: str; project_id: str; session_id: str
    src_id: str                # e.g. a hypothesis
    dst_id: str                # e.g. an evidence it rests on
    relation: Literal["supported_by", "contradicted_by",
                      "derived_from", "assumes"]
    weight: float              # source-reliability-adjusted (§5.1)
    created_at: str
```

`Recommendation` additionally denormalizes its rationale for cheap read-back:
`based_on_hypotheses: list[id]`, `based_on_evidence: list[id]`, `unresolved_assumptions: list[id]`
— but these are **derived from** `BeliefEdge` (edges are the source of truth; recompute on write).

### 3.2 Scoping — why `session_id`

A belief graph is scoped to a **reasoning session** = (agent, project, objective). This bounds the
graph (revision is cheap), keeps provenance answerable, and avoids premature "org knowledge base"
complexity. Tenant isolation is RLS as everywhere in `ros`. Cross-session reuse is future work.

### 3.3 Integrity — dedup, referential integrity

- **Referential integrity:** an edge may only reference claims in the same `session_id` (FK +
  check). Deleting a claim is forbidden (see §3.4); orphan edges are impossible by construction.
- **Dedup:** `content_hash = sha256(normalize(statement))` is unique per `(session_id, type)`.
  Exact-match dedup is free. **Semantic** dedup ("Wi-Fi step is painful" ≈ "users struggle at
  Wi-Fi config") is *not* solved by hashing — v1 flags near-duplicates via embedding cosine >
  threshold for the LLM to merge in `update_beliefs`; it does **not** auto-merge (a wrong merge
  corrupts provenance). Logged as an open question (§9).
- **Idempotent tool writes:** re-running a resolver after a crash writes the same `content_hash` →
  no duplicate evidence (the resume-safety invariant, same as artifact-storage's content-addressing).

### 3.4 Revision history — append-only, never destroy

Contradiction and revision **never delete**. A claim that is falsified transitions
`active → invalidated` (or `→ superseded` with `superseded_by`), retaining its edges. This is what
makes the backward-walk honest ("we believed X, then analytics contradicted it") and gives a free
audit trail — which is exactly the `ros` governance story. Reads default to `status IN (active,
promoted)`; history is available on request.

### 3.5 What lives in LangGraph state vs the store

Checkpoint state holds only: `session_id`, the current `objective`, `critical_unknowns`,
`available_tools`, and a **working-set of claim ids** in play this turn. The graph itself
(claims + edges + history) is in the store. This keeps checkpoints small and makes the belief graph
outlive any single run — the artifact-storage principle applied to reasoning.

---

## 4. Organ 2 — belief-revision & confidence-propagation policy

**Design decision (load-bearing):** the **LLM proposes structure** (new claims, and which edges of
which `relation` connect them); a **pure, deterministic `RevisionEngine` computes all confidence.**
No LLM runs inside the propagation. This makes revision **testable, reproducible, and
order-independent**, and confines model unreliability to a place we can verify (§5.6).

### 4.1 Confidence from evidence — bounded log-odds accumulation

A hypothesis's confidence is **not an average** of its evidence (averaging lets weak corroboration
dilute strong contradiction). Use additive **log-odds**, which is commutative (order-independent —
§4.4) and naturally handles support vs contradiction as opposite signs:

```
logit(c) = log(c / (1-c))

For hypothesis H with edges e_i (weight w_i in [-1, 1] after §5.1 signing):
    L(H) = logit(prior_H) + Σ_i  w_i * logit(strength(e_i))
    confidence(H) = clamp(sigmoid(L(H)), c_min, c_max)     # e.g. [0.02, 0.98] — never certain
```

- `supported_by` contributes `+`, `contradicted_by` contributes `−` (edge weight sign).
- `strength(e_i)` is the **evidence's own calibrated confidence** (§5) — provenance-weighted.
- `c_min/c_max` bounds forbid false certainty. A single source can never drive a hypothesis to 1.0.

### 4.2 Cascade — recommendations recompute when hypotheses move

Recommendations are the sinks of the DAG. Their confidence is a function of the hypotheses they rest
on **and** their unresolved assumptions:

```
confidence(R) = min_over(based_on_hypotheses via same log-odds)  ×  assumption_penalty
assumption_penalty = Π (1 - risk(a))   for each active unresolved assumption a
```

`min_over` (weakest-link) is deliberate: a recommendation is only as strong as its shakiest
supporting hypothesis. When any hypothesis crosses a threshold, **propagate deterministically** in
topological order and re-stamp dependents:

```
on new_claims/new_edges:
    1. attach edges (LLM-proposed structure)
    2. recompute confidence of directly-touched hypotheses (§4.1)
    3. topological walk downstream: hypotheses -> recommendations
    4. any recommendation whose confidence dropped below `stale_threshold`
       -> status = "stale"  (NOT deleted; surfaced for review)
    5. any assumption contradicted by evidence with strength > τ
       -> status = "invalidated"; recurse from its dependents
```

This is the "revise, don't restart" behavior made mechanical — and the cascade-invalidation the
source sketch never specified.

### 4.3 Assumption lifecycle

```
active ──(supporting evidence, strength > promote_τ)──▶ promoted   (treated ~ as fact; still traceable)
active ──(contradicting evidence, strength > kill_τ)──▶ invalidated (cascades to dependents §4.2)
```

An assumption never silently becomes a fact — promotion is an explicit, logged transition with the
promoting evidence recorded, so provenance stays intact.

### 4.4 Properties we guarantee (and test)

- **Order-independence:** evidence arriving in any order converges to the same confidences
  (log-odds addition is commutative). Property test in §7.
- **Contradiction is preserved, not resolved by deletion:** both `supported_by` and
  `contradicted_by` edges coexist; confidence reflects the tension (the Wi-Fi case: qualitative
  support + analytics contradiction → weakened, not erased).
- **Monotone provenance:** adding evidence never orphans a claim or loses history.
- **Determinism:** `RevisionEngine.apply(graph, delta)` is a pure function — same inputs, same graph.

---

## 5. Organ 3 — calibrated uncertainty

**Premise:** LLM self-reported confidence is uncalibrated and must never directly gate a decision.
Confidence enters the system through **four disciplined channels**, in priority order:

### 5.1 Source-reliability priors (config, tunable)

Each `source_type` carries a reliability weight used to sign/scale edges (§4.1). Configured, not
guessed; overridable per `AgentProfile`:

```
experiment        0.95     # A/B result with stats
analytics         0.90     # behavioral, quantified
support (aggregate)0.75    # "31% of 837 tickets" — weighted by sample size (§5.2)
document          0.70     # product docs / decisions
support (single)  0.40     # one ticket
user              0.50     # stakeholder assertion — important but not ground truth
agent_inference   0.30     # the model's own hypothesis — lowest, must be corroborated
```

### 5.2 Structural confidence — derive from data, not vibes

Where a claim carries structure, compute confidence from it instead of asking the model:

- "31% of **N=837** tickets mention Wi-Fi" → confidence from sample size / proportion CI, not an LLM
  float. `confidence_basis = {"method": "structural", "n": 837, "p": 0.31}`.
- Effect sizes, funnel deltas, experiment p-values → mapped through fixed functions.
- This is the **highest-trust** channel and is preferred whenever the source is quantitative.

### 5.3 Adversarial verification for high-impact claims (calibration without outcomes)

Before a claim with `decision_impact = high` is allowed to *raise* a recommendation's confidence,
run an **independent refutation pass** (a second model call prompted to refute, defaulting to
"refuted" under uncertainty). Only claims that survive contribute full weight; refuted-but-plausible
claims are retained at damped weight with the refutation attached as a `contradicted_by` edge. This
is how we get trustworthy confidence *before* any real-world outcome exists — mirroring the
platform's own adversarial-verify pattern.

### 5.4 Calibration layer (closes the loop when outcomes arrive — optional)

Raw model self-confidence, when we must use it (`agent_inference` with no structure), is passed
through a **calibration map** (Platt/isotonic) fit against realized outcomes as they accumulate.
Until enough outcome data exists, the map is the identity clamped to the conservative
`agent_inference` prior. This makes the doc-1 "observed outcome" signal exactly what it should be —
**optional, and it improves calibration when present** — never a prerequisite.

### 5.5 Presentation — bands at the boundary, provenance always attached

- Internally: float. **At any decision boundary and in any user-facing output: bands**
  (`low / medium / high`) to avoid false precision, keyed off calibrated confidence.
- **No number is ever shown without its provenance** (`confidence_basis` + source). A human can
  always discount a claim by seeing where it came from.

### 5.6 Why this is trustworthy

Model unreliability is confined to *structure proposal* (which claims exist, which edges connect
them) — cheap for a human to eyeball via the backward-walk — while every *number* comes from configured
priors, sample statistics, adversarial survival, or fitted calibration. The deterministic
`RevisionEngine` (§4) does the arithmetic. There is no step where "the LLM said 0.65" gates an action.

---

## 6. Orchestration (thin LangGraph) & the real loop

### 6.1 Corrected topology

```
                 ┌───────────────────────────────────────────────┐
   user/trigger ─┤                                                 │ new context arrives
        │        ▼                                                 │ (tool connected,
        ▼   understand_context ─▶ prioritize_unknowns              │  answer, doc, metric)
   (load session,     │                                            │
    belief graph)     ▼                                            │
              inspect_capabilities ─▶ gather_evidence ──▶ update_beliefs
                                          │  (LLM proposes    (RevisionEngine — pure §4)
                                          │   claims/edges)         │
                                          ▼                         ▼
                                   decide_progress ◀────────────────┘
                                     │           │
                          can_make_progress   blocking unknown remains
                                     ▼           ▼
                                produce_work   ask_user  ── interrupt() ──▶ (pause; resume on answer)
                                     │
                                     ▼
                                  output (+ provisional flags, open questions)
```

Two fixes vs the source: (a) `ask_user` uses LangGraph **`interrupt`** and the checkpointer, so the
run genuinely **pauses and resumes** on the human answer rather than falling off `END`; (b) new
context (a tool becoming available, a stakeholder reply, a metric appearing) re-enters at
`prioritize_unknowns` against the **persisted** graph — the same architecture resolves more of the
state (the T0→T3 enrichment, now actually wired).

### 6.2 Capability resolution (unchanged in spirit, wired to `ros`)

`gather_evidence` does `unknown → available capability → best resolver` against the **`ros` tool
registry**, filtered by the **authz gate** and the `AgentProfile.runtime_env` (progressive tool
availability = provisioning more resolvers). The `InformationNeed` record (`decision_impact`,
`blocking`, `resolver`, `ask_human`) is retained verbatim — it's the operational form of "important
≠ blocking."

---

## 7. Integration with `ros` primitives

| PM-harness concept | `ros` primitive |
|---|---|
| The PM agent (governed subject) | **`AgentProfile`** (owns the belief graph; `agent_id` scope) |
| "Why do you believe this?" backward-walk | **audit / governance** surface (the wedge) |
| Belief store reads/writes | new `pm:belief_read` / `pm:belief_write` **authz permissions** |
| Recommendation actions (later) | separately gated `pm:recommend` / world-writes behind the tool gate |
| Tool registry `good_for` selection | **tool materialization + authz gate** |
| Progressive tool availability (T0→T3) | **`AgentProfile.runtime_env`** / provisioning |
| Durable pause/resume for `ask_user` | LangGraph **checkpointer + interrupt** (already in stack) |
| Tenant isolation | **Postgres RLS** (mandatory floor) |

New authz entries (default-deny registry): `pm:belief_read` → `viewer`, `pm:belief_write` →
`editor`, `pm:recommend` → `editor`. World-mutating resolvers stay `admin`+ and off by default.

## 8. Test strategy

- **RevisionEngine (pure) — property tests:** order-independence (shuffle deltas → identical graph);
  weakest-link cascade; contradiction preserved; bounds never breached; assumption lifecycle
  transitions.
- **Calibrator (pure):** structural confidence from known (n, p); band boundaries; source-prior
  application; identity-until-fitted calibration map.
- **Store:** RLS isolation; edge referential integrity; content-hash dedup; append-only history
  (no destructive update).
- **Loop (integration):** cold-start (no tools) still produces a provisional output with open
  questions; adding a tool re-resolves an unknown without restart; `interrupt`/resume round-trip.
- **Backward-walk golden test:** the Wi-Fi scenario end-to-end reproduces the doc-10 reconstruction
  (recommendation → hypothesis → supporting + contradicting evidence + outstanding assumption).

## 9. Open questions

- **Semantic dedup / merge:** when is auto-merge safe? Wrong merges corrupt provenance. Default:
  never auto-merge; LLM proposes, engine records a `superseded_by` only on explicit confirmation.
- **Source-reliability learning:** move §5.1 priors from config to learned-from-outcomes — needs
  enough closed-loop outcome data first.
- **Cross-session belief reuse:** promote a per-session finding to a project-level belief — scoping,
  staleness, and governance implications unresolved.
- **Conflicting `user` assertions** from different stakeholders — whose assertion wins, and does
  authority weight the prior? (The doc-1 multi-stakeholder gap.)

## 10. Phasing

- **P1 — Store + RevisionEngine + Calibrator (pure core).** Schema, RLS, deterministic policy,
  structural + prior confidence, full test suite. No LLM, no loop. This is the risky unknown and it's
  independently verifiable.
- **P2 — Thin loop.** LangGraph nodes as structured-output calls over the store; `interrupt`/resume;
  capability resolution against the tool gate; cold-start → provisional output.
- **P3 — Calibration + adversarial verify.** §5.3 refutation pass; §5.4 calibration map wired to
  outcomes when they exist.
- **P4 — Governance surface.** Backward-walk UI / API on the audit spine; `pm:*` permissions;
  gated recommendation actions.

Build P1 first — it's where the three organs live, and it stands alone without the model in the loop.
