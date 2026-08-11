# Secure multi-tenant execution — isolating data plane (WS10)

**Context / threat model:** hosting **untrusted third-party agents and code at multi-tenant scale**.
Assume every workflow, prompt, tool definition, and code snippet is hostile, and that prompt
injection will make agents attempt whatever the attacker wants (call tools, exfiltrate data, spend
money, reach internal services). Under that model, ROS's current execution (whole-run-per-**worker**
on a shared Railway host, with **app-level** SSRF guard and Postgres **RLS via a `app.current_tenant`
GUC**) is **insufficient** — untrusted in-process code can bypass an app-level egress check and set
GUCs. This spec defines the hardened data plane that fixes that.

## Non-negotiables (what "hard isolation" must guarantee)
1. **No shared-host code execution.** A run's agent loop + tools + code execute in a **per-run
   sandbox** (microVM/gVisor), with CPU/mem/disk/time caps and no host access.
2. **No ambient credentials in the sandbox.** The sandbox never holds the master key, the DB
   connection, or another tenant's secrets. It gets only **short-lived, run-scoped** creds.
3. **No direct data access from the sandbox.** No raw DB handle, no raw object-store creds. All
   privileged data access goes back through the control plane, tenant-scoped **server-side** (RLS
   via a GUC the untrusted code could set is not a boundary here).
4. **Network egress default-deny**, enforced at the **network layer** (not just the app), with an
   allow-list (LLM providers + the workflow's declared tool endpoints); block metadata IP
   (169.254.169.254), RFC1918, localhost.
5. **Hard resource + spend caps** per run and per tenant; fair scheduling so one tenant can't
   starve the fleet or the wallet.

## Architecture — control plane vs data plane
```
            ┌─────────────────────────── Control plane (Railway) ───────────────────────────┐
 client ──► │ auth · RBAC/governance · CRUD · run ADMISSION (quota/budget/model allow-list)  │
            │ records/traces · secrets MANAGEMENT (master key) · dispatch · scoped data API  │
            └───────▲───────────────────────────────────────────────────────┬───────────────┘
     scoped run token│  (state/secrets/tool-proxy over a tenant-scoped API)   │ dispatch run + scoped creds
            ┌────────┴───────────────────── Data plane (sandbox fleet) ───────▼───────────────┐
            │  per-run isolated VM: compile_workflow → run graph (deep_agent + crew + tools)   │
            │  egress firewall (allow-list) · CPU/mem/time caps · NO db/master-key/other tenants│
            └────────────────────────────────────────────────────────────────────────────────┘
```
- **Control plane** never runs untrusted code. It holds the master key, does admission + governance,
  and exposes a **tenant-scoped callback API** the sandbox uses for state, secrets-on-demand, and
  privileged tool calls.
- **Data plane** = a fleet of ephemeral sandboxes. Plugs into the existing execution-backend seam
  (`ros.execution_backends`): today's `local` (arq/worker) is the *trusted* backend; a new
  `sandbox` backend is the *hardened* one.

## Execution lifecycle — ephemeral VM + checkpointer (no long-lived VM)
Tie the sandbox to **active compute**, not wall-clock (a run may pause on HITL for minutes–days):
1. Control plane admits the run (quota/budget/model allow-list), mints a **short-lived run token**,
   resolves **only this run's** scoped creds.
2. Dispatch to a sandbox (warm pool to hide cold-start). Sandbox compiles + runs the graph.
3. On **interrupt** (`human_input`/`handoff`) or completion, state is in the **Postgres checkpointer**
   (via the control plane); the sandbox is **torn down**.
4. On **resume**, a **fresh** sandbox is spawned and state rehydrated from the checkpointer.
→ Arbitrarily long/HITL runs cost nothing while paused, and a compromised sandbox is short-lived.

## Secrets & egress (the crux)
- **Secrets:** control plane resolves the run's provider keys + referenced tool creds, and either
  (a) injects them into the sandbox as **short-lived** values, or (better, later) (b) keeps raw keys
  out entirely and injects auth at an **egress proxy** — the agent/code gets *handles*, the proxy
  attaches real credentials on allow-listed calls (the Naïve "handles, never raw secrets" model).
- **Egress:** network-level default-deny + allow-list. The existing `EgressPolicy`/SSRF guard stays
  as defense-in-depth but is **not** the boundary — the sandbox's network is.
- **Data:** sandbox has **no DB/vector/object-store handle**. Checkpoint reads/writes, KB retrieval,
  and artifact I/O go through the **tenant-scoped callback API** (identity from the run token,
  enforced server-side). Artifacts use per-tenant bucket prefix + scoped creds (BucketResolver).

## Reuse what we already have
- **Execution-backend seam** (`ros.execution_backends`) — the plug point for the `sandbox` backend.
- **Whole-run-per-worker** (`run_to_completion`) — same shape, sandbox instead of worker process.
- **Postgres checkpointer** — durability across ephemeral sandboxes (already the model).
- **Governance (WS9)** — per-run/tenant/day spend hard-caps + model allow-list at admission +
  the pre-action `tenant_budget` gate. Add per-tenant **concurrency** caps + sandbox CPU/mem/time.
- **Freestyle code executor** — the inner tier: untrusted **code** inside a run still goes to a
  code sandbox (two-level isolation: run-VM → code-exec), not shared with the run's process.
- **Reaper** — extend to reclaim stuck/expired sandboxes.
- **RLS** — keep for the control plane's own DB access; do **not** rely on it as the untrusted-code
  boundary (the sandbox has no DB access at all).

## Sandbox provider options (decision needed)
| Option | Isolation | Ship speed | Cost @ scale | Ops | Notes |
|---|---|---|---|---|---|
| **E2B** | Firecracker microVM | fast (SDK, purpose-built for agent/code) | per-run | low | Best fit for "untrusted agent + code sandboxes." |
| **Modal** | gVisor/containers (+GPU) | fast | per-run | low | Great for compute/GPU; container isolation. |
| **Fly Machines** | Firecracker microVM | medium (API) | per-run/CPU-sec | medium | Full VMs via API; you assemble the fleet. |
| **Freestyle** | Linux VMs (agent workspaces) | medium | unknown (verify) | low | Works, but agent-workspace-oriented; billing/persistence undocumented. |
| **Self-host Firecracker / gVisor** | strongest / cheapest at scale | slow | lowest | **high** | Bare-metal + orchestration; only worth it at large scale. |

**Decision (2026-08):** **E2B** is the selected provider for the Phase-1 `sandbox` backend (managed
Firecracker microVMs, purpose-built for agent/code sandboxes — fastest path to hard isolation).
Keep the door open to self-hosted Firecracker later if volume justifies the ops. Freestyle remains
the **code-exec** inner tier (already integrated). **Status: spec only — not scheduled for build yet.**

## Phasing
- **Phase 0 — cheap hardening (do first, no new infra).** Enforce that runs with untrusted
  code/tools use the isolating **code executor** (Freestyle) not `restricted`; add **network-level**
  egress allow-list where the worker runs; per-tenant **concurrency** caps; confirm the sandbox
  path never carries the master key; keep code tools **off by default** for untrusted tenants until
  Phase 1. Documents the residual risk honestly.
- **Phase 1 — `sandbox` execution backend.** Whole-run-per-microVM (E2B) via `ros.execution_backends`;
  ephemeral-VM + checkpointer lifecycle; short-lived run token; tenant-scoped callback API for
  state/secrets/tools; egress firewall; sandbox CPU/mem/time caps. Behind a per-project/tenant flag.
- **Phase 2 — egress credential proxy** (handles, not raw keys) + **warm pool** + fair-scheduling +
  per-tenant quotas surfaced in governance.
- **Phase 3 — enterprise:** dedicated/BYO isolation (per-tenant VPC/region), self-host Firecracker
  option, hash-chain audit (WS5 5d) for tamper-evidence.

## Honest cost/latency note
Hard isolation is not free: per-run microVM cold-start (mitigated by a warm pool) and per-run cost
vs. an always-warm shared worker. That's the price of safely running untrusted multi-tenant code —
and the reason the trusted `local` backend stays the default for first-party/single-tenant projects.
