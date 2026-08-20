# Freestyle run-execution — live findings & the untrusted-isolation decision (G2 follow-up)

**Status:** findings + decision · 2026-08 · no further execution-path code until agreed
**Scope:** what the deployed `freestyle` execution backend actually does on real VMs, why it cannot
meet the untrusted-multi-tenant isolation goal as written, and which path we take next.
**Read with:** `design/secure-multitenant-execution.md` (the authoritative hardened-data-plane spec,
E2B chosen), `GAPS.md` G1/G2, `forge/docs/standalone-runtime-split-spec.md`, `ros/execution/
freestyle.py`, `ros/execution/freestyle_control.py`, `ros/runtime/driver.py`.

## What is now live (Railway `forge`/production)

- `freestyle-svc` deployed (Fastify run-control service); healthy on `:8080`, reachable from api +
  worker over `freestyle-svc.railway.internal`.
- Snapshot `ros-claude-backend` baked and boot-verified: `python -m ros.runtime`, `ros` pkg,
  `claude_agent_sdk`, and the `claude` CLI all resolve. `ROS_SNAPSHOT_ID` set on the service.
- api + worker wired: `ROS_EXECUTION_BACKEND=freestyle`, `ROS_FREESTYLE_SERVICE_URL`,
  `ROS_FREESTYLE_WARM_VMS=true`, matching `ROS_FREESTYLE_SERVICE_SECRET`.

So G2's "run the bake + wire it up" and "live-verify the SDK shapes" are **done** — the control plane
(auth, VM create/reuse/persistent lifecycle, teardown, detached exec, snapshot boot) is verified
end-to-end against the real platform.

## What the live workflow run proved (the real blocker)

Ran an actual workflow (`start → agent → claude_code → end`) through the production `submit()` →
freestyle path. Result:

- **Dispatch works:** the run recorded `executor={"driver":"freestyle","vm_id":...}`; a VM booted
  from the snapshot; `python -m ros.runtime drive --run-id ...` is a valid command.
- **The run never progressed** — stuck `queued`, `heartbeat=None`, for the full watchdog window. VM
  log: `sqlite3.OperationalError: no such table: runs`.

### Root cause (two layered bugs, both in the backend — not in freestyle-svc)

1. **Shared creds were never injected into the VM.** `freestyle_control.dispatch_run` sent only
   `ROS_MASTER_URL` + `ROS_RUNTIME_TOKEN`. The VM's `drive` therefore fell back to its baked, empty
   local SQLite → `no such table: runs`. Fixed by injecting `ROS_DATABASE_URL` / `ROS_REDIS_URL` /
   `ROS_SECRET_KEY` / checkpointer settings (commit in `freestyle_control.py`).

2. **…but that fix cannot actually work in this deployment, and shouldn't for our goal.** The shared
   Postgres + Redis are on Railway **private** hosts (`postgres.railway.internal`,
   `redis.railway.internal`) that a Freestyle VM (outside Railway's network) cannot reach. Making it
   work would require **exposing Postgres + Redis publicly** and handing the VM full DB/Redis creds.

## Why "expose the DB/Redis to the VM" is the wrong direction

The stated purpose of running workflows on VMs is to **isolate untrusted / user-supplied code**
(`claude_code`, code tools) from the platform — a security boundary. Under that threat model
(`secure-multitenant-execution.md`: assume every workflow/prompt/tool is hostile):

- RLS in this codebase is **app-enforced** (an `app.current_tenant` GUC set by the app; `set_current_
  tenant` is a no-op on SQLite). It is **not** a connection-level boundary. Untrusted code on a VM
  holding the DB password can set/bypass the GUC and read/write **every tenant's** data, secrets, and
  checkpoints.
- Same for Redis: full access to the cross-tenant relay bus + rate-limit/lock/revocation state.

So the current `driver.py` **trusted-VM model** — which by design "holds shared DB + Redis + secret-
key creds, injected at provision time" — is fundamentally a *scale-out-of-trusted-code* mechanism,
**not** an untrusted-isolation mechanism. Exposing the DB to satisfy it would defeat the whole point.

This matches the non-negotiables already written in `secure-multitenant-execution.md`: *no ambient
credentials in the sandbox*, *no direct data access from the sandbox* (all privileged access goes back
through a tenant-scoped, server-side API).

## Decision

1. **Keep `freestyle` (trusted-VM, direct-DB) as an explicit interim / scale-out backend only.** It is
   acceptable for **first-party / single-tenant / trusted** projects where offloading heavy runs off
   the api box is the goal. Keep the cred-injection fix so it *functions* wherever the VM can reach the
   shared DB/Redis (e.g. same-VPC, or public-TLS DB with trusted code). It is **not** an
   untrusted-isolation boundary and must be gated to trusted tenants — mirroring the G1 guidance for
   `claude_code`.

2. **The untrusted-isolation path is the `sandbox` backend already spec'd in
   `secure-multitenant-execution.md` (E2B, Phase 1), NOT this Freestyle trusted-VM path.** Its data
   plane is the manifest/callback model: the sandbox gets only a short-lived run token + this-run-only
   scoped creds, holds **no** DB/master-key, and does all privileged data access (state, secrets,
   tools, checkpoints) through a **tenant-scoped control-plane API**. That is the correct answer to
   "the VM can't reach the DB" — the sandbox should never touch the DB at all.

3. **`driver.py` today implements only the direct-DB path.** The manifest/callback driver referenced
   in the split spec ("the manifest endpoint + run token remain for the stricter, DB-less isolation
   option") is **not built**. Building it is the real work, and it belongs to the E2B `sandbox`
   backend effort, not to Freestyle.

## Consequences of the "API/manifest" path (so we go in eyes-open)

It is the right long-term architecture for untrusted code, but it is a project, not a config flag:

- **Re-implements the driver data plane over HTTP:** manifest fetch (workflow + input + this-run-only
  resolved secrets), plus `POST` endpoints for status / heartbeat / lease / result / tokens / trace
  spans / checkpoints, plus frame publish (or keep Redis for the relay only).
- **Secret delivery becomes the crux:** the manifest carries *decrypted* per-run creds (the graph
  can't compile without them). Blast radius shrinks (one run vs. whole DB) — the upside — but the run
  token becomes a high-value credential: must be run-bound, short-TTL, revocable, audited, and every
  new endpoint must enforce "this token may touch only this run."
- **Re-opens stream/finalize divergence** — the exact thing the shared-`_drive` design avoids today.
  Two write paths (local `_finalize` vs. API) will drift (frame shapes, token accounting, span
  structure, HITL state) unless carefully contract-tested.
- **Checkpoint/HITL/resume over HTTP** is the fiddliest piece (LangGraph durability assumes a real
  Postgres checkpointer); needs an HTTP checkpoint shim correct under concurrency.
- **Chattiness/latency + snapshot-vs-api schema versioning** (a snapshot baked against an older
  manifest contract can silently break) — needs contract versioning + rebake discipline.

## Immediate posture

- Config left as-is (`freestyle` selected) per request, even though untrusted runs won't complete on
  it yet; the trusted-VM path only completes where the VM can reach the shared DB/Redis.
- Do **not** expose Postgres/Redis publicly to satisfy the trusted-VM path for untrusted tenants.
- Next execution-path work targets the E2B `sandbox` backend (Phase 1 of
  `secure-multitenant-execution.md`) + its manifest/callback data plane — after this decision is
  agreed.
