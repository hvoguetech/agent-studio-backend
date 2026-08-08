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

Checkpoint after each WS item (don't batch merges); each backend change must keep the full
`pytest` suite green + `ruff` clean before deploy.
