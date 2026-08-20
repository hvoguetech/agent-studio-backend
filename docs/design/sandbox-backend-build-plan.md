# Build plan — the isolating `sandbox` execution backend (WS10 Phase 1)

**Status:** build plan (design-first) · 2026-08
**Parent spec:** `design/secure-multitenant-execution.md` (authoritative: control-plane vs data-plane,
non-negotiables, E2B decision, phasing). This doc maps that spec onto the code that **already exists**,
names the exact gaps, and sequences the build. Also read: `freestyle-run-execution-findings.md` (why
the trusted-VM path is interim-only), `GAPS.md` G1/G2.

## Goal (restated from the parent spec)
Run a whole workflow for an **untrusted / multi-tenant** tenant inside a **per-run ephemeral sandbox**
that holds **no ambient authority**: no master key, no DB/Redis handle, no other tenant's secrets. The
sandbox gets only a **short-lived, run-scoped token** and reaches all privileged state (input,
this-run-only secrets, status/result, trace spans, checkpoints) through a **tenant-scoped control-plane
API**. Network egress is default-deny at the network layer.

This is explicitly **NOT** the trusted-VM `driver.py` path (that hands the VM the shared DB + master
key — a scale-out mechanism for trusted code, kept as an interim backend only).

## What already exists (reuse — do not rebuild)
The manifest/control-plane split is **partially built**. Inventory:

| Piece | Where | State |
|---|---|---|
| Backend seam (`ExecutionBackend`, lazy resolve) | `ros/execution/base.py`, `registry.py` | ✅ done — `sandbox` plugs in here via `ros.execution_backends` |
| Run-scoped token (`runtime:pull`, run+tenant bound, TTL, revocable) | `ros/security.py:create_run_token` / `decode_token` | ✅ done |
| Manifest endpoint (token-gated, RLS-bound) | `ros/routers/runtime.py` `GET /v1/runtime/runs/{id}/manifest` | ✅ done |
| Manifest builder (workflow + tools + agents + skills + **run-scoped resolved secrets/provider keys**) | `ros/services/runtime_manifest.py` | ✅ mostly — secret refs + provider keys resolved; MCP + auth-provider secrets are follow-ups |
| DB-less context rebuild | `ros/services/runtime.py` `build_compile_context_from_manifest` | ✅ done |
| Manifest-pull CLI + runner | `ros/runtime/__main__.py` (`run`), `runtime/runner.py`, `runtime/client.py` | ⚠️ exists but (a) writes checkpoints to **shared Postgres**, (b) **non-streaming** (`ainvoke`), (c) no status/result callback |
| Provisioned per-(agent,end_user) env into the process | `ros/runtime/env.py`, manifest `runtime_env` | ✅ done |
| Governance hard-caps mirrored into the manifest | `runtime_manifest.py` (`tenant_budget` prepend) | ✅ done |
| Freestyle VM control service (`/run`, boot/reuse/teardown, snapshot) | `freestyle-svc/`, `ros/execution/freestyle_control.py` | ✅ done (G2) — reusable as the sandbox *dispatcher* if we stay on Freestyle; E2B is the spec's provider |

**Takeaway:** the *read* half of the data-plane split (pull a manifest, rebuild context DB-less) is
built. The **write half is missing**: the sandbox cannot report status/result/frames/checkpoints
without a DB handle. That write-back API is the core of this build.

## The gaps to close (this is the work)

### G-A. Control-plane callback API (the write half) — NEW
The sandbox must push everything back over HTTP, run-token-authenticated, RLS-enforced **server-side**
(never trust a tenant id from the sandbox — take it from the token, exactly as `runtime.py` already
does for the manifest). New endpoints under `/v1/runtime/runs/{run_id}/…`, all gated by the run token
(extend the scope, e.g. `runtime:drive`):
- `POST .../frames`      — append SSE frames (relay to the browser via the existing relay bus, so
                            live node progress still works). Ordered, replay-safe.
- `POST .../status`      — `running` + heartbeat/lease stamps (feeds the reaper + the stuck-run
                            watchdog already in `RunService`).
- `POST .../result`      — terminal: status (done/error/interrupted), answer, tokens, cost, spans.
- `POST .../checkpoint`  — read/write LangGraph checkpoint blobs for HITL/resume (see G-C).
Server-side these call the SAME `RunService` finalize/relay primitives the local path uses, so there
is **one** finalize (avoid the stream/finalize divergence called out in the findings doc).

### G-B. A callback-mode driver on the sandbox — NEW (or refit `runner.py`)
Today `runner.py` = manifest + `ainvoke` + shared-Postgres checkpointer. The sandbox needs a driver
that:
- streams (reuses `map_chunk_frames`, the shared mapper — same frames as master),
- posts frames/status/result via G-A instead of touching the DB,
- uses a **callback-backed checkpointer** (G-C) instead of `AsyncPostgresSaver`.
New CLI verb: `python -m ros.runtime sandbox --run-id … --master-url … --token …` (keep `run`/`drive`
for the offline/trusted modes). No `ROS_DATABASE_URL`/`ROS_SECRET_KEY`/`ROS_REDIS_URL` in the sandbox
env — that is the whole point.

### G-C. Callback-backed checkpointer — NEW
A `BaseCheckpointSaver` whose get/put go through G-A's `.../checkpoint` endpoint (control plane writes
Postgres). Needed for HITL/resume durability without a DB handle in the sandbox. This is the fiddliest
piece; MVP can support non-HITL runs first (no interrupt) and add checkpoint proxying second.

### G-D. `SandboxBackend` on the execution seam — NEW
`ros/execution/sandbox.py` (a `ros.execution_backends` plugin, NOT in the MIT core). `submit()`:
admits the run (quota/budget/model allow-list — already in `create_run`/`run_admission`), mints the
run token, dispatches to the provider (E2B per spec; Freestyle control-svc reusable interim), records
`executor`. Interactive + trigger paths already call `get_backend().submit()`, so wiring is free.
Selected by `ROS_EXECUTION_BACKEND=sandbox`, behind a per-project/tenant flag.

### G-E. Network egress default-deny (network layer) — provider config
Per non-negotiable #4: deny by default, allow-list LLM providers + the workflow's declared tool
endpoints, block 169.254.169.254 / RFC1918 / localhost. This is E2B/microVM network config, not app
code. `EgressPolicy`/SSRF guard stays as defense-in-depth (`manifest["egress"]` already carried).

### G-F. Provider = E2B (per the spec's decision)
The parent spec selects **E2B** (Firecracker microVMs, purpose-built). Freestyle's `freestyle-svc` is
reusable as an interim dispatcher, but E2B is the target for hard isolation + warm pools. Keep the
dispatcher behind a thin interface so provider swap is config, not a rewrite.

### G-G. Secret-delivery follow-ups
Manifest already carries run-scoped resolved secret refs + provider keys. Remaining (from
`runtime_manifest.py` TODOs): MCP config + secrets, and auth-provider-mediated tool creds. Phase-2 of
the parent spec replaces raw-secret injection with an **egress credential proxy** (handles, not keys)
— out of scope for Phase 1 but the endpoints in G-A should not preclude it.

## Phasing (small, shippable, reversible)
1. **P1a — callback API + sandbox driver, non-HITL. ✅ BUILT (2026-08).** G-A callback endpoints
   (`POST .../frames|status|result` in `ros/routers/runtime.py`, run-token-gated like the manifest),
   G-B streaming callback driver (`ros/runtime/sandbox.py` + `python -m ros.runtime sandbox` +
   `MasterCallback` in `runtime/client.py`), G-D `SandboxBackend` (`ros/execution/sandbox.py`,
   registered as `sandbox`; dispatch via `freestyle_control.dispatch_sandbox_run` which injects ONLY
   `ROS_MASTER_URL` + the run token — no DB/Redis/master-key). `_vm_dispatch_enabled()` + the
   redis-relay config guard extended to `sandbox`. Non-HITL only: an interrupt finalizes as `error`
   with a clear message (checkpoint proxying is P1b). Tests: `tests/test_sandbox_backend.py` (incl. the
   cred-omission invariant + callback auth). **Not yet run end-to-end against a live sandbox VM** — the
   snapshot's `ros` package must include these files (rebake needed) before a live smoke test.
2. **P1b — checkpoint proxying (G-C) → HITL/resume** on the sandbox.
3. **P1c — E2B provider + network egress default-deny (G-E/G-F)** + warm pool for cold-start.
4. **P2+** — egress credential proxy (handles), fair scheduling, per-tenant concurrency/CPU/mem caps,
   enterprise isolation. (Parent spec Phases 2–3.)

## Boundaries / non-negotiables to hold throughout
- Sandbox env NEVER contains `ROS_SECRET_KEY`, `ROS_DATABASE_URL`, `ROS_REDIS_URL`, or another
  tenant's secrets. (Contrast the trusted-VM `driver.py`, which does — that path stays separate.)
- Tenant identity is ALWAYS taken from the run token server-side; never from a sandbox-supplied field
  (RLS via a sandbox-set GUC is not a boundary).
- One finalize / one frame-mapper shared with the local path (no divergence).
- Every new callback endpoint enforces "this token may touch only this run" (mirror `runtime.py`).

## First code step (once this plan is agreed)
G-A `POST /v1/runtime/runs/{id}/frames` + `.../status` + `.../result`, token-gated like the manifest
endpoint, calling the existing relay-bus + `RunService` finalize primitives — then G-B/G-D to drive one
non-HITL workflow end-to-end on a sandbox with zero DB creds. No sandbox execution-path code lands
before this doc is agreed.
