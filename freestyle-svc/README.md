# ros-freestyle-svc

The **run-control service** ROS's `freestyle` execution backend dispatches to. It boots the ros
runtime (`python -m ros.runtime drive`) on a **Freestyle VM** and returns a receipt; the VM drives the
run against the shared Postgres + relay bus. See `apps/api/ros/execution/freestyle_control.py` (the
client) and `forge/docs/standalone-runtime-split-spec.md` (the design).

## VM lifecycle policy (chosen)

**A VM runs until it is EXPLICITLY torn down** (`DELETE /vm/:vmId`). VMs are created with
`persistence: "persistent"` and no idle timeout — nothing auto-suspends or recycles them. One
persistent VM per agent: `/run` reuses the live VM for a `stickyKey` if present, else creates one.
A finer eviction policy (idle GC, cost caps, per-tenant limits) is deferred (docs/GAPS.md **G2**).

## Endpoints

All but `/healthz` require `Authorization: Bearer <FREESTYLE_SERVICE_SECRET>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness |
| POST | `/run` | boot/reuse a VM and launch the ros runtime; body `{ runId, tenantId, projectId?, command, env?, stickyKey?, warm? }` → `{ vm_id, runId, reused }` |
| GET | `/run/:vmId` | status `{ vm_id, alive, record }` |
| DELETE | `/vm/:vmId` | **explicit teardown** (the only thing that destroys a VM) |

## Develop

```bash
npm install
FREESTYLE_API_KEY=... FREESTYLE_SERVICE_SECRET=... npm run dev
```

## Wire ROS to it

On the ROS **api** + **worker** services:

```
ROS_EXECUTION_BACKEND=freestyle
ROS_FREESTYLE_SERVICE_URL=http://<this-service>:3000
ROS_FREESTYLE_SERVICE_SECRET=<same as FREESTYLE_SERVICE_SECRET here>
ROS_FREESTYLE_WARM_VMS=true   # send stickyKey=workflow_id so one persistent VM is reused per agent
ROS_REDIS_URL=...             # required by the backend's prod guard when a control service is set
```

## Bake the `ros-claude-backend` image

For a real run, a VM needs Python + the `ros` package + the `claude` CLI/SDK. `build-image.ts` bakes
a Freestyle snapshot named **`ros-claude-backend`** with all of that, verifies a fresh VM booted from
it can run `python -m ros.runtime` and `claude`, and prints the snapshot id.

```bash
npm run build
ROS_INSTALL_REPO_URL="https://github.com/hvoguetech/agent-studio-backend.git" \
ROS_INSTALL_TOKEN="$(gh auth token)" \
ROS_INSTALL_REF="main" \
FREESTYLE_API_KEY=... FREESTYLE_SERVICE_SECRET=... \
npm run build:image
# → prints ROS_SNAPSHOT_ID=<id> — set that on this service.
```

Set the printed `ROS_SNAPSHOT_ID` on the service; `/run` then boots VMs from the snapshot (fast, the
runtime is already inside). Without a snapshot, `/run` boots a base Python VM that does NOT have the
`ros` package, so `python -m ros.runtime` won't resolve — the control-plane logic (auth, VM
create/reuse/persistent lifecycle, teardown) is still exercisable, but a real run needs the snapshot.

Requires a Freestyle plan that supports snapshots + a GitHub token that can clone the (private) repo.
