# Known gaps — to revisit

A running log of known limitations / deferred work discovered during implementation. Each entry
records the gap, why it exists, its impact, and the intended fix so we can pick it up later. This is
distinct from `ROADMAP.md` (product direction) — these are specific, concrete debts.

---

## G1 — `claude_code` node has no hard multi-tenant isolation

- **Area:** execution / security — `ros/nodes/claude_code.py`
- **Status:** open · deferred
- **Severity:** high for untrusted multi-tenant hosting; low for trusted/single-tenant use

### Gap
The `claude_code` node wraps the Claude Agent SDK, which spawns the `claude` CLI subprocess and does
**real filesystem + shell work** in a working directory (`cwd`). When the node runs on the master /
control-plane process, that work happens on the shared host with only app-level guards — it is **not**
hard-isolated per tenant. Under the threat model in `design/secure-multitenant-execution.md` (assume
every workflow/prompt/tool is hostile), this is insufficient for hosting untrusted third-party agents.

### Why it exists
The hardened data plane — a per-run microVM (E2B) sandbox on the `ros.execution_backends` seam with
network-level egress default-deny, short-lived run-scoped creds, and no ambient host access — is
**spec'd but not built** (see `design/secure-multitenant-execution.md`, "Status: spec only — not
scheduled for build yet"). The node was shipped transport-only to be useful in trusted/single-tenant
setups now, without blocking on that data plane.

### Current mitigations (partial)
- The governed Anthropic key is injected into the subprocess env only for the run's duration and
  removed after (no leak into the long-lived process env).
- `permission_mode` / `allowed_tools` / `disallowed_tools` bound what the agent may do.
- The node is transport-only: it reads `ROS_CLAUDE_CODE_WORKSPACE` for its `cwd`, so it already runs
  inside the per-run VM workspace when executed on the Freestyle/E2B execution backend — no node change
  needed once that path lands.

These are **defense-in-depth, not an isolation boundary.** The `cwd` is still on the host FS, the
network is not layer-enforced, and shell/edit tools are real.

### Intended fix
Run the node inside the **`sandbox` execution backend** (Phase 1 of
`design/secure-multitenant-execution.md`): whole-run-per-microVM, ephemeral-VM + checkpointer
lifecycle, tenant-scoped callback API for state/secrets/tools, network egress firewall, and CPU/mem/
time caps. Until then, gate the node to trusted tenants/projects (do not expose to untrusted
multi-tenant callers), mirroring how code tools are off by default for untrusted tenants (Phase 0).

### References
- `docs/design/secure-multitenant-execution.md` (control plane vs data plane; E2B decision; phasing)
- `ros/execution/base.py` (`ExecutionBackend` seam), `ros/execution/freestyle.py`, `ros/runtime/`
- `ros/services/providers/base.py:81` (note: a run-level sandbox lands on the execution seam, not the
  provisioning seam)

---

## G2 — freestyle-svc (VM control service) & VM lifecycle policy

- **Area:** execution / infra — `freestyle-svc/` + `ros/execution/freestyle_control.py`
- **Status:** in progress · vertical slice being built
- **Severity:** blocks the `freestyle` execution backend from doing anything (falls back to `local`)

### Gap
`ROS_EXECUTION_BACKEND=freestyle` dispatches runs to a control service at `ROS_FREESTYLE_SERVICE_URL`
via `POST /run` (`freestyle_control.py`), but **no such service existed** — so the backend silently
fell back to the in-process `local` path. The intended service (spec: `forge/docs/standalone-runtime-
split-spec.md`, Parts F/G) boots the ros runtime (`python -m ros.runtime drive`) on a Freestyle VM.

### VM lifecycle decision (chosen)
**A VM runs until it is EXPLICITLY torn down** (`DELETE /vm/:id`) — no idle-suspend, no auto-recycle.
Enforced at VM-create time by the control service: `persistence: "persistent"`, no
`idleTimeoutSeconds`. This is configurable via env (`ROS_VM_PERSISTENCE`, default `persistent`); note
that Freestyle's `sticky` mode is NOT sufficient for this policy (it suspends on idle). A finer
lifecycle/eviction policy (idle GC, cost caps, per-tenant VM limits) is DEFERRED — to be added later.

### VM reuse
One persistent VM per agent: `/run` reuses the live VM for a given `stickyKey` (= workflow id) if it
exists, else creates one. `warm`/`stickyKey` are sent by ROS when `ROS_FREESTYLE_WARM_VMS=true`.

### Ros-runtime snapshot image — `ros-claude-backend` (addressed)
`freestyle-svc/src/build-image.ts` (`npm run build:image`) bakes a Freestyle snapshot named
**`ros-claude-backend`** with Python + the `ros` package (prod extras) + the `claude` CLI + the
Claude Agent SDK, verifies a fresh VM booted from it runs `python -m ros.runtime` and `claude`, and
prints `ROS_SNAPSHOT_ID`. `/run` boots VMs from that snapshot when the id is set (snapshot-OPTIONAL:
without it, a base Python VM boots that lacks the `ros` package). Running the bake needs a Freestyle
plan with snapshots + a token that can clone the repo — that execution step is still TODO.

### Done (2026-08, verified live on Railway `forge`/production)
- **Bake + wire-up + live-verify:** `build:image` ran against a real Freestyle account
  (`ROS_SNAPSHOT_ID` set); `freestyle-svc` deployed and healthy; api + worker pointed at it. The
  Freestyle SDK create/exec/snapshot shapes (incl. detached `vm.exec` + snapshot readiness) are
  confirmed. Control plane (auth, VM create/reuse/persistent lifecycle, teardown, snapshot boot) works
  end-to-end.
- **`ros-claude-backend` snapshot:** additionally fixed the `claude` CLI to install deterministically
  (symlink the pip-bundled `claude_agent_sdk/_bundled/claude` onto PATH; npm's platform binary was a
  flaky optionalDependency). And the clone token is now a separate `ROS_INSTALL_TOKEN` (not embedded
  in the URL).

### Blocker found by a live workflow run (see `design/freestyle-run-execution-findings.md`)
Dispatch works (run gets `executor={"driver":"freestyle","vm_id":...}`, a VM boots), but the run
stayed `queued` (`no such table: runs` in the VM). Two layers:
1. `dispatch_run` never injected the shared DB/Redis/secret creds → the VM fell back to an empty local
   SQLite. **Fixed** in `freestyle_control.py` (inject `ROS_DATABASE_URL`/`ROS_REDIS_URL`/
   `ROS_SECRET_KEY`/checkpointer).
2. …but the shared Postgres + Redis are on Railway **private** hosts a Freestyle VM cannot reach, and
   making the trusted-VM model work would mean **exposing the DB + handing the VM full creds** — which
   defeats the untrusted-isolation goal (RLS here is app-enforced, not a connection boundary).

### Decision
- Keep `freestyle` (trusted-VM, direct-DB) as an **interim / scale-out backend for trusted /
  single-tenant projects only** — gate it like G1's `claude_code`.
- The **untrusted-isolation** path is the E2B `sandbox` backend already spec'd in
  `design/secure-multitenant-execution.md` (Phase 1) with its manifest/callback data plane — the
  sandbox holds **no** DB/master-key and reaches state/secrets/tools only through a tenant-scoped
  control-plane API. `driver.py` today implements only the direct-DB path; the manifest driver is the
  real remaining work and belongs to that effort, not to Freestyle.
- Do **not** expose Postgres/Redis publicly to force the trusted-VM path for untrusted tenants.

### References
- `freestyle-svc/` (this service), `ros/execution/freestyle_control.py` (the `/run` client)
- `forge/docs/standalone-runtime-split-spec.md` (Parts A–G; the authoritative design)
- Atlas `freestyle-svc` (a different product's service; used as a proven scaffold template)
