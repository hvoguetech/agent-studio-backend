# Runbook: LIVE-VERIFY the Freestyle VM + Redis execution path (ROS #4)

The interactive-on-VM path (FreestyleBackend → freestyle-svc → standalone `ros.runtime` driver →
shared Postgres, with the live token stream relayed back through master over a shared Redis) is built
and unit-tested with in-memory doubles, but has **never run against real `freestyle-svc` + Redis**.
Code marked `LIVE-VERIFY` stays unverified until this runbook is executed once on a real deploy.

This gates the "runs for real" claim. No further feature work changes it — it needs infra + a smoke run.

## 1. Stand up the infra

### freestyle-svc
- Deploy the control service and set `ROS_FREESTYLE_SERVICE_URL` (+ `ROS_FREESTYLE_API_KEY` if it
  requires auth) on the **master** app.
- Confirm the `/run` contract matches `ros/execution/freestyle_control.py::dispatch_run`:
  request carries `{run_id, tenant_id, project_id, master_url, run_token, public, run_context}` and,
  in warm mode, `{stickyKey, warm:true}`; the receipt returns a `vm_id`. **If the deployed shapes
  differ, reconcile `dispatch_run` — this is the one contract the suite can't check.**
- Verify warm/sticky reuse: with `ROS_FREESTYLE_WARM_VMS=true`, two runs of the same agent
  (sticky_key = workflow id) reuse one warm VM instead of cold-booting.

### Shared Redis (reachable by master AND the VM)
- Master reaches it over Railway's private network; the **VM is outside that network**, so it needs a
  **public `rediss://` URL with AUTH**. Set `ROS_REDIS_URL` accordingly on master and provision the
  same URL into the VM env (below).

### VM env provisioning at boot
The VM's standalone runtime needs, injected at provision time:
- `ROS_DATABASE_URL` — the shared Postgres (state is written here; master reads the same DB).
- `ROS_REDIS_URL` — the public `rediss://…` bus (relay of the live stream back to master).
- `ROS_SECRET_KEY` — to mint/verify tokens consistent with master.
- Checkpoint vars: `ROS_CHECKPOINT_BACKEND=postgres` and `ROS_CHECKPOINT_POSTGRES_URL`
  (or reuse `ROS_DATABASE_URL`) — so an interrupted (HITL) run is durable + resumable across a VM
  restart and visible to master.

> Per-end-user isolation (2b): the agent's own provisioned resource env (`DATABASE_URL`, `REDIS_URL`,
> endpoint URLs for the resources the agent provisioned) is **separate** from the VM infra vars above.
> It is resolved per-run scoped to `(Run.agent_id, end_user)` and exported into the run process by the
> runtime entrypoint (`ros/runtime/env.py::apply_runtime_env`). Verify below that a keyed run sees it
> and that two end users of the same agent see different values.

## 2. Automated smokes (this repo)

```bash
cd apps/api
# Readiness report — validates the env above without connecting.
./.venv312/bin/python -m scripts.freestyle_smoke preflight

# RedisRelayBus against the REAL Redis: publish → LPUSH/LTRIM buffered replay → Last-Event-ID tail →
# live pub/sub delivery. (Add --dry-run to validate the logic offline with the in-memory bus.)
ROS_REDIS_URL="rediss://…" ./.venv312/bin/python -m scripts.freestyle_smoke redis-relay
```

`preflight` must print **READY**; `redis-relay` must print **PASS** against the real Redis.

## 3. Console/API smokes (need a real VM + a seeded workflow)

Set `ROS_EXECUTION_BACKEND=freestyle` on master, then, against a project with a configured workflow:

1. **Dispatch an interactive run** — POST `/v1/projects/{id}/run` (stream) or run from the console.
   Expect: the token stream relays through master's SSE, the Traces view shows a `VM · {vm_id}` badge,
   and the run row finalizes (`status=done`, answer/tokens) in the shared DB.
2. **Cancel mid-run** — cancel while streaming. Expect: master commits `status=canceled`; the VM's
   in-run watcher stops it and relays a `canceled` terminal frame.
3. **Kill the VM mid-run** — terminate the machine. Expect: the relay watchdog
   (`run_orphan_threshold_seconds`, default 90) surfaces a terminal error frame; the reaper then
   reclaims the orphaned run from its checkpoint. (See #8: reclaim currently re-drives on master.)
4. **Per-end-user creds (2b)** — dispatch two runs of the same agent as different end users, each
   provisioned a private resource. Expect: each run's process sees its own `DATABASE_URL` (the
   agent-shared value overridden by the end user's private one); neither sees the other's.

## 4. Sign-off

Record the results (dates, vm_ids, any `dispatch_run` contract fixes) and remove the `LIVE-VERIFY`
markers in the touched files once green:
- `ros/execution/freestyle_control.py` (dispatch `/run` + stickyKey/warm)
- `ros/services/run_relay.py` (`RedisRelayBus` pub/sub + LPUSH/LTRIM replay)
