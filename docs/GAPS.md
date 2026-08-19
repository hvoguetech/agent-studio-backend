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
