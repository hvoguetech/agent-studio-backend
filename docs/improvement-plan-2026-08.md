# Improvement plan — post-B/E4 (2026-08)

Roadmap for the four workstreams chosen after shipping B/E4 (default-deny authz) and the
prod hardening/port fixes. Ordered by risk/leverage: **WS1 → WS2 → WS4**, with **WS3** run by
the parallel session. Each item lists scope, effort, risk, and done-criteria.

Status legend: ☐ todo · ◐ in progress · ☑ done

---

## WS1 — Ops & security quick wins  *(start here; low risk, fast)*

- ☐ **Rotate transcript-exposed secrets.** JWT, service token, bootstrap admin password, Postgres
  & Redis passwords (surfaced in a debugging transcript).
  - Regenerate → update the `ROS_*` vars / regenerate the Railway Postgres+Redis plugin creds →
    redeploy. **Impact:** rotating `ROS_JWT_SECRET` invalidates live sessions (everyone re-logs
    in) — use `ROS_JWT_SECRET_PREVIOUS` for a zero-downtime overlap window.
  - Done: new secrets live; old values invalid; a login still works.
- ☐ **#55 — `prefer_ipv4_egress` on IPv6-only hosts.** Default is `true` (IPv4-first DNS patch).
  Works for Railway's private net today, but IPv6-only *external* egress can break. Make the
  behaviour explicit/safe and document `ROS_PREFER_IPV4_EGRESS=false` for IPv6-only networks.
  - Done: documented + a guard/log when egress fails under the patch.
- ☐ **#56 — lazy provider imports.** `import ros.main` eagerly pulls the full LangChain/LangGraph/
  LiteLLM + provider SDK stack → slow cold start + high memory on small instances. Defer heavy
  imports to first use; keep the warm-up daemon thread.
  - Done: cold-boot import time + RSS measurably lower; full test suite green.
- ☐ **Delete `forge/.mcp.json`** (live ROS key on disk in the retired monorepo) + rotate that key.
  Sandbox blocks `rm` of the working dir → user runs `rm -rf /Users/marutsingh/hvogue/forge`.
- ☐ **Scaling readiness (only if going >1 api replica):** set `ROS_SECRET_KEY` (Fernet, from a
  secret store) so replicas share the master key, and switch `ROS_VECTOR_BACKEND=chroma → pgvector`.

## WS2 — OpenFGA fine-grained / ReBAC authz  *(#22 — unblocked by B/E4)*

The `authorize(subject, permission)` chokepoint already exists; this plugs a real engine behind it.

- ☐ Provision an **OpenFGA service** on Railway (+ its own Postgres store).
- ☐ Author the **authorization model** (types: `org`, `tenant`, `project`, `resource`; relations:
  owner/admin/editor/viewer/member; parent-child).
- ☐ Implement an **OpenFGA-backed engine** selected by `ROS_AUTHZ_ENGINE` (default `role_tier`,
  opt-in `openfga`) — `authorize()` calls `check()`; callers unchanged.
- ☐ **Tuple sync** on tenant/project/membership create/delete (write relationship tuples).
- ☐ **Rollout:** shadow mode (log divergences vs role-tier) → enforce. Coverage/parity tests.
- Effort: L. Risk: M (new infra + a wrong model can lock people out — shadow first).

## WS3 — Finish the canvas bijection  *(Phase 1c/1d — run by the parallel session)*

- ☐ **Save-wiring** in `apps/web/components/screens/workflows.tsx`: thread `settings`/`entry_node`
  from `canvasToFlow` → state → `canvasToExecutable` on save (lib already supports it; #6ee67b9).
- ☐ **Edge inspector UI** — edit `edge.data.condition` + `branches`.
- ☐ **Workflow-settings panel** — `error_policy`, `on_error`, `timeout_seconds`, `max_concurrency`.
- ⚠️ **Ownership:** the other session is editing `graph.ts`/`workflows.tsx`. Coordinate before
  touching these files (see `docs/design/canvas-bijection-handoff.md`). Default: they own WS3.

## WS4 — Enterprise foundation

- ☐ **#11 B/E1 — Org/Account layer above Tenant.** New `Organization` parent over `Tenant`
  (workspace); org-level membership, billing, SSO anchor. Schema + migration + RLS + APIs.
  Foundational — B/E2/B/E3/B/E7 and A/C8 hang off it. Effort: L. Risk: M (core schema migration).
- ☐ **#15 B/E5 — Tamper-evident audit log + SIEM.** Hash-chain / signed audit entries, enforced
  append-only on SQLite too, streaming export (Datadog/Splunk/S3), and read-auditing. Effort: M.

## WS5 — Compliance & isolation

Enterprise/regulatory + untrusted-code-execution gaps. Not soft-launch blockers for a small
trusted user set, but **GDPR triggers on the first EU/California user**, and **sandboxing gates
whether code tools can ever be enabled for untrusted tenants**.

### 5a. Execution sandboxing — isolated code executor

> **Architecture:** [`docs/design/code-execution-sandbox.md`](design/code-execution-sandbox.md)
> (CodeExecutor seam + restricted/subprocess/container tiers + rollout).

**Current state (verified in `ros/tools/code.py`):** code tools run under **RestrictedPython**
(AST-level — blocks dunder/`eval`/`exec`/`import` escapes, guarded builtins). Same engine gates
router/`expressions.py` value decisions. It is a **hardening layer, NOT an OS sandbox**:

- ❌ no CPU/memory/wall-clock bound; a runaway thread **cannot be force-killed** (in-process DoS).
- ❌ no OS/process isolation (no subprocess/container/gVisor/seccomp).
- ✅ **Safe by default today:** `enable_code_tools=False`, and the production guard **refuses to
  boot** if code tools are on without `allow_unsandboxed_code_tools=true` (explicit RCE/DoS
  acknowledgement). `enable_mcp_stdio` (arbitrary local command exec) is off by default too.

**Rule until fixed:** do NOT enable code tools (or MCP stdio) for untrusted / multi-tenant users.

- ☐ Build an **isolated executor**: run code-tool bodies in a subprocess/container (or the
  deep-agent sandbox backend) with CPU/memory/wall-clock limits (`resource.setrlimit` +
  timeout/kill), network egress via the existing SSRF policy, and a hard output cap. Wire it
  behind the existing `enable_code_tools` flag; keep RestrictedPython as defence-in-depth.
- Done: a runaway/malicious code tool is killed at the resource limit and cannot exhaust or
  crash the api process; code tools become safe to enable for untrusted tenants.
- Effort: L. Risk: M (new execution path; needs its own resource/security tests).

### 5b. GDPR — data-subject export + right-to-erasure  *(#18 B/E8)*

**Current state:** `PortabilityService` exports *config* (tools/workflows/agents), **not** user
data. Per-user connection delete + cascading workspace delete exist, but there is **no
per-subject data export and no right-to-erasure** over conversation / PII / trace data.

- ☐ Subject data **export** (all rows tied to an end-user/email across conversations, traces,
  runs, connections) as a portable bundle.
- ☐ Right-to-**erasure** (targeted delete/anonymise for a subject) + a retention policy for
  conversation/PII data (today's retention is time-based, not subject-targeted).
- **Trigger:** required the moment you onboard EU (GDPR) / California (CCPA) users. Effort: M.

### 5c. Platform-wide DLP / PII policy  *(#19 B/E9)*

**Current state:** PII redaction is **opt-in per-agent** middleware, not platform-enforced.
✅ Trace redaction (`trace_tool_io_redact`) already **auto-enables in production**, so sensitive
headers/cookies are masked in prod traces.

- ☐ Platform-wide, admin-set DLP/PII policy applied to all agents (not per-agent opt-in);
  content-safety scan on the memory write-path (relates to M5 #52). Effort: M.

### 5d. Tamper-evident audit log + SIEM  *(#15 B/E5)*

**Current state:** audit is append-only by convention + Postgres RLS; there is an `export_audit`
endpoint but **no cryptographic tamper-evidence**.

- ☐ Hash-chain / sign audit entries (enforced append-only on SQLite too), streaming export to a
  SIEM (Datadog/Splunk/S3), and read-auditing. Effort: M. *(same as WS4 #15 — consolidate there
  or here.)*

## WS6 — Scale-out (concurrent workflows)

Run hundreds+ of concurrent workflow runs. The engine is built around a pluggable
`ExecutionBackend`; this WS is about operating the **default Redis/arq backend** at scale and
keeping the **Inngest** durable backend as a documented option.

### 6.0 Execution-granularity decision (locked)

**Work unit = the whole run, not the node.** A worker job runs `run_to_completion(run_id)` —
the entire graph in one process, node→node hops in memory; the Postgres checkpointer persists
each superstep for durability/resume. Only `code` nodes leave the process (→ Freestyle, WS5 5a).

We deliberately do **NOT** distribute at the node level by default. Node-level durable-step
distribution (Inngest/Temporal-style) adds ~tens–hundreds of ms **per hop** (enqueue + state
load/save + worker pickup) — fine for long/durable workflows, but it dominates latency on tight
agent loops and cheap nodes (routers/transforms) where there's no LLM call to hide behind.
In-process hops are ~µs. So: **run-level distribution for throughput + inline for interactive
latency; node-level is opt-in per workflow class only** (see 6.4).

### 6.1 Topology — two stateless tiers + a queue

- **api** replicas: HTTP + interactive/SSE runs (execute **inline**; a stream is pinned to its
  replica). Scale on CPU / active SSE connections.
- **worker** replicas: consume `run_job` from Redis, whole-run-per-worker, `arq max_jobs`
  concurrent (I/O-bound → set ~15–30). Scale on **Redis queue depth**.
- Throughput ≈ `worker_replicas × max_jobs` (offloaded) + `api_replicas × per-proc cap`
  (interactive). Both tiers stateless → scale = add replicas.

- ☐ **Deploy the `worker` service** (`arq ros.worker.WorkerSettings`; `workers`+`postgres`
  extras; `ROS_REDIS_URL`; `PORT` n/a). **Also fixes a live bug:** with Redis set but no worker,
  trigger/webhook/schedule runs are enqueued to Redis and **never executed** (they stall).
- ☐ Autoscale: workers on queue depth, api on CPU/SSE count.

### 6.2 Multi-replica prerequisites

Done: Postgres checkpointer ✅ · Redis shared state ✅ · `ROS_SECRET_KEY` ✅ · singleton
leader-election for scheduler/reaper/retention ✅.
- ☐ **`chroma → pgvector`** — HARD blocker: chroma is single-writer on a volume; 2+ replicas
  touching knowledge corrupt/diverge. `ROS_VECTOR_BACKEND=pgvector` + `CREATE EXTENSION vector`
  (+ re-embed existing knowledge). Do this before scaling api/worker past 1.

### 6.3 Ceilings & fixes (in the order they bite)

1. ☐ **Postgres connections** (first wall): replicas × pool can exhaust `max_connections` →
   add **PgBouncer** (transaction pooling); size `db_pool_size`/overflow deliberately.
2. ☐ **Checkpointer write QPS** (a write per superstep): keep `run_durability=async`; scale
   Postgres; later split the checkpointer onto its own DB.
3. ☐ **LLM provider rate limits + cost** (true external ceiling): provider quota / multiple
   keys; lean on the existing budget + quota admission + `max_concurrent_runs_per_tenant`.
4. ☐ **Fairness:** arq is a single FIFO queue → one tenant's burst can head-of-line-block others.
   Per-tenant caps/quotas already exist; add **per-tenant queues / priority** if it bites.

### 6.4 Backend option — Redis/arq vs Inngest (keep both)

Selected by `ROS_EXECUTION_BACKEND` (`local` default; a plugin resolves via the
`ros.execution_backends` entry-point). Callers are unchanged either way.

| | **`local` (Redis/arq)** — this repo, MIT | **`inngest`** — cloud edition (separate private plugin, EPIC D/#36) |
|---|---|---|
| Work unit | whole run per worker (in-proc hops) | durable steps (can distribute/retry per step) |
| Latency | **lowest** (µs inter-node) | higher per step (queue+state per hop) |
| Durability | checkpointer (superstep resume) | native per-step durability + replay |
| Retries/backoff | run-level (arq) + dead-letter | per-step, built-in |
| Long waits / HITL | checkpoint + re-enqueue (slot freed) | native durable sleep/wait |
| Fan-out / cron | in-proc + singleton scheduler | native fan-out + scheduling |
| Ops burden | you run workers + Redis | managed (or self-host Inngest, SSPL) |
| Best for | interactive + hundreds of independent runs | very long/durable workflows, huge fan-out, per-step SLAs |

- ☐ **Default: stay on `local`** (Redis/arq, run-level) — right latency/ops trade for interactive
  agents and independent-run throughput.
- ☐ **Keep `inngest` as an opt-in** behind the seam for the workflow classes that need durable
  steps; the interactive SSE path stays on the inline `local` path regardless (Inngest can't
  stream tokens). Decision to adopt is per-workflow-class, not global.
- Legal note: self-hosted Inngest server is **SSPL** — confirm with counsel for a managed offering
  (see [[forge-execution-architecture]] open flag).

### 6.5 Checkpoint retention (TTL cleanup)

The Postgres checkpointer persists a checkpoint per superstep, but `RetentionService.purge_expired`
only ages out traces/spans/runs — **checkpoints are cleaned only on project/workspace delete**, so
they **grow unbounded** for the life of a project (cost + bloats the DB the run hot-path writes to).
This is the Postgres equivalent of DynamoDB-TTL checkpoint expiry.

- ☑ Extend the retention sweep to delete checkpoints for **fully-expired threads** — a thread whose
  runs are ALL terminal (done/error/canceled) *and* whose newest run is past the retention horizon.
  Never delete a thread with a live/resumable run (queued/running/interrupted), and never a partial
  conversation thread that still has runs after the cutoff. Uses the same `checkpointer.adelete_thread(lg_thread_id)`
  path as project delete; leader-gated + idempotent like the rest of the sweep.
- Related (later): optional checkpoint→S3 offload for genuinely-large *state* (WS7 handles large
  *artifacts* via refs-in-state). gzip isn't needed — Postgres TOAST already compresses large values.

## WS7 — Artifact storage

Durable, downloadable storage for agent/tool-produced files, kept OUT of run state (design:
[`docs/design/artifact-storage.md`](design/artifact-storage.md)). One shared bucket, isolated by a
`{env}/{tenant}/{project}/{run}/{sha}` key prefix; content-addressed (idempotent, resume-safe);
refs-in-state + bytes-in-store; `BucketResolver` seam for enterprise dedicated/BYO buckets.

- ☑ **Phase 1 — storage layer.** `ObjectStore` (local default + s3), `BucketResolver`,
  `ArtifactStore` (key scheme, content-addressing, size cap), config (`ROS_ARTIFACT_STORE`/`_BUCKET`/
  `_MAX_BYTES`, `ROS_S3_*`), `[storage]` extra (boto3, lazy). Tested (local round-trip, idempotency,
  traversal-safety, resolver, s3 shaping mocked).
- ☑ **Phase 2 — `Artifact` model + API.** DB table (tenant/project/run/key/sha/size/content_type) +
  migration `0012_artifacts`; upload/list/download(presign or streamed)/delete router gated by
  `artifact:read/write` (added to the registry). `ObjectStore.delete_object` for per-artifact delete.
- ☐ **Phase 3 — producers + GC.** Wire the deep-agent filesystem backend + a tool-artifact emit path
  to `ArtifactStore` (refs-in-state); ref-aware retention + cascade delete; per-tenant storage quota;
  egress allowlist for the bucket endpoint.
- ☐ **Phase 4 — enterprise.** Dedicated/BYO/region buckets via a custom `BucketResolver`; per-tenant KMS.

## WS8 — Typed node I/O

Turn node I/O from metadata-only into an enforceable contract, without changing the shared-state
backbone or the canvas↔executable bijection (design:
[`docs/design/typed-node-io.md`](design/typed-node-io.md)). All fields optional + backward-compatible;
runtime enforcement defaults to observe/warn.

- ☑ **(a) Runtime output-schema enforcement.** `NodeInstance.output_schema` is now *enforced*: the
  compiler validates a node's primary output value (`primary_output_key` in `ros/engine/node_io.py`)
  against it, reusing `tools/output_schema.py`. `output_schema_strict` raises (composes with
  `error_handling.on_error`); otherwise observe + `nodes.output_schema_mismatch` metric.
- ☑ **(a2) Runtime input validation.** `input_schema` is enforced at runtime too: `enforce_input_schema`
  validates the incoming state (projected to the schema's keys) before the node runs; observe +
  `nodes.input_schema_mismatch`, `input_schema_strict` raises (composes with `on_error`). Catches bad
  entry/trigger inputs. Symmetric with output enforcement.
- ☑ **(b) Build-time contract.** Validator: `Edge.mappings.to` must be a declared state key (error),
  `from` valid JMESPath (error), mapping on a control edge warns; malformed `output_schema`/
  `input_schema` warn; opt-in producer→consumer presence contract (`input_schema.required`),
  field+type contract on mapped edges, and a **plain-edge contract** (A.output_schema vs B.input_schema
  when B names A's output key); `io_type` warning suppressed when a mapping bridges the edge.
- ☑ **(c) Edge field-mapping.** `Edge.mappings[{from,to}]` in the schema + compiler support
  (source-side fold, no topology change → bijection preserved).
- ☑ **Frontend (agent-studio-frontend).** Edge-inspector "Data mapping" rows, node output/input
  schema editors (+ strict toggles), a Structured-JSON-output (`response_format`) control on
  llm/agent, and an opt-in "infer input from upstream" button.
- ☑ **Export to LangGraph Studio.** `GET …/workflows/{id}/export/langgraph` returns a `langgraph dev`
  project (langgraph.json + graph.py + executable.json + README/.env/requirements) that reconstructs
  the compiled graph via `compile_workflow` for local debugging; toolbar "LangGraph" download button.
  Models resolve from env keys; tool_call/retrieval degrade offline. See `ros/services/langgraph_export.py`.
- ☐ **Enhancement — infer schema from a test run.** Populate a node's output/input schema from an
  actual run's observed values (reuse `output_schema.infer_schema`, as tools do). Spec:
  [`docs/specs/enhancement-infer-schema-from-test-run.md`](specs/enhancement-infer-schema-from-test-run.md).
- ☐ **Enhancement — first-class `code` node.** A graph node running sandboxed Python via the existing
  `execute_code` seam (restricted/freestyle), like `transform` but for code. Infra (sandbox tiers,
  gating) already exists for the code *tool*; needs a node factory + schema + form. Not yet filed.
- ☐ **Later.** Array-item/`$ref`/complex-JMESPath type reconciliation in the field-type check.

## WS9 — Agent-infra parity (model routing · governance · Supabase)

Prompted by a competitive read of usenaive.ai. Focused, high-leverage parity items.

- ☑ **Model routing.** `openrouter:<vendor/model>` gateway ref → OpenAI-compatible client at
  OpenRouter (300+ models + OpenRouter's own routing/fallback), key from `ROS_OPENROUTER_API_KEY`
  or project `provider_credentials.openrouter`. Project **model aliases** (`fast`/`smart`/… →
  concrete refs) resolved in `resolve_model` (`CompileContext.model_aliases`). Exception-triggered
  cross-model fallback already exists via the `model_fallback` middleware.
- ☑ **Governance hard-caps.** Admission now checks **every per-node model** against `allowed_models`
  (`enforce_project_budget(..., executable=)`), not just the project default. A project
  `budgets.max_usd_per_run` (or `max_tokens_per_run`) is **auto-injected** as a `tenant_budget`
  middleware so the per-run cost cap is a **hard, pre-action (before_model) stop** on every agent by
  default. (`_TenantBudgetMiddleware` gained a run-scoped `max_usd_per_run` channel.)
- ☑ **Supabase data layer.** Postgres + pgvector + S3-compatible Storage run via env only — see
  [`docs/deploy-supabase.md`](deploy-supabase.md) (incl. the pooler/prepared-statement caveat).
- ☐ **Later.** Latency/rate-limit-aware routing (not just exception fallback); non-run spend
  (embeddings/tools) summed into the monthly cap; unknown-model `$0` pricing blind spot; hash-chain
  audit (tamper-evidence, also WS5 5d); optional Supabase-Auth (OIDC/GoTrue) `ROS_AUTH_BACKEND` seam.

## WS10 — Secure multi-tenant execution (isolating data plane)

Required IF hosting **untrusted third-party agents/code at multi-tenant scale**: process isolation
on a shared worker is insufficient, and app-level SSRF + RLS-via-GUC are not boundaries once
untrusted code runs in-process. Design:
[`docs/design/secure-multitenant-execution.md`](design/secure-multitenant-execution.md).

- ☐ **Phase 0 — cheap hardening (no new infra).** Force the isolating code executor (Freestyle) for
  untrusted code; network-level egress allow-list; per-tenant concurrency caps; ensure the exec path
  never carries the master key; code tools off by default for untrusted tenants.
- ☐ **Phase 1 — `sandbox` execution backend.** Whole-run-per-microVM (**provider: E2B, selected**) via
  `ros.execution_backends`; ephemeral-VM + checkpointer lifecycle; short-lived run token +
  tenant-scoped callback API for state/secrets/tools (sandbox holds NO db/master-key); egress
  firewall; CPU/mem/time caps. Behind a per-tenant flag. Trusted `local` backend stays the default.
- ☐ **Phase 2 — egress credential proxy** (handles, not raw keys) + warm pool + fair-scheduling +
  per-tenant quotas in governance.
- ☐ **Phase 3 — enterprise:** dedicated/BYO VPC/region isolation, self-host Firecracker option,
  hash-chain audit.

---

## Sequencing & checkpoints

1. **WS1** now — self-contained, testable in `apps/api/.venv312`, no infra. (Secret rotation is
   the one disruptive item — schedule it deliberately.)
2. **WS2** next — needs an infra decision (deploy OpenFGA) before coding; ship behind a flag in
   shadow mode first.
3. **WS4** after — org-layer migration is the biggest blast radius; do it with snapshots + a
   reversible migration.
4. **WS3** in parallel via the other session.
5. **WS5** is trigger-driven, not calendar-driven: **5a (sandboxing)** only before enabling code
   tools for untrusted users; **5b (GDPR)** before the first EU/California user; **5c/5d** for
   enterprise/regulated deals. Until then: keep code tools off (default) and note the gaps in any
   security questionnaire.
6. **WS6** when concurrency climbs (or before enabling triggers): first **deploy the worker
   service** (also fixes stalled triggered runs) and **chroma→pgvector**, then scale replicas +
   PgBouncer. Stay on the `local` (Redis/arq) backend; treat Inngest as a later opt-in per §6.4.

Checkpoint after each WS item (don't batch merges); each backend change must keep the full
`pytest` suite green + `ruff` clean before deploy.
