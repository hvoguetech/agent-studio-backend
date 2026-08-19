# Artifact plane + Code node — design spec

**Status:** Draft / proposal (2026-08-19) · **Target repo:** `agent-studio-backend` (package `ros`)
· **Related:** `docs/design/code-execution-sandbox.md` (the stateless code *tool*), `docs/specs/provisioning-dx.md` (#6 provisioning).

> Plan-only. §5 decisions are **settled** (2026-08-19); still awaiting a starting slice (§6) before implementation.

---

## 1. Motivation

Two capabilities the workflow engine doesn't have today:

1. **Artifact exchange between nodes** — pass *files/blobs* (a report, a diff, build output) from one node to the next, not just text/JSON riding the shared state.
2. **A "code node"** — an agent that checks out a git repo into an isolated workspace and *builds on it* (Claude-Code-style: read/write/edit files, run a shell, run git/builds), handing its outputs to downstream nodes as artifacts.

The two are coupled: the code node's cross-node handoff *is* the artifact plane, so artifacts are the foundation and land first.

## 2. Goals / non-goals

**Goals**
- A first-class **artifact plane**: nodes produce/consume file artifacts **by reference**; bytes live **outside** the LangGraph checkpoint.
- A new **`code` node kind**: repo checkout + edit/bash/git/build loop in an **isolated workspace**; outputs recorded as artifacts (branch, commit sha, diff, logs).
- **Reuse** existing isolation/governance: governed-subject capability (`ApiKey`), `tenant_budget` hard-cap, egress policy, provisioned storage/compute (`provision_resource`).

**Non-goals (this effort)**
- Replacing the existing **code *tool*** (`ros/tools/code.py`, RestrictedPython) — it stays for pure compute/glue.
- Moving the **graph itself** onto a per-node VM — execution stays **per run** (`ExecutionBackend`); only the *workspace* is isolated (see §4.3).
- IDE/LSP features. We ship a **shell + filesystem + git** workspace, nothing more.

## 3. Current architecture (what we build on)

| Seam | File | Reality today |
|---|---|---|
| Graph state channels | `ros/engine/state.py` | Dynamic `TypedDict`; types (`list[message|str|json]`, scalars) + reducers (`add_messages`, `add`, `merge`, `last`). **No file/artifact channel.** |
| Node I/O routing | `ros/engine/node_io.py` | Data flows via shared state; edge `mappings` copy a JMESPath src→target key; `primary_output_key` per node type. |
| Node-type registry | `ros/engine/registry.py` | `register(NodeSpec(type, ports, factory, …))`; compiler + validator + UI palette all read it. **New node = one `register()`.** |
| Compile context | `ros/engine/context.py` | Carries `sandbox`, `sandbox_backend_for()`, `runtime_env`, `agent_id` (governed subject), `end_user`, `egress_policy`, `checkpointer`. |
| Sandbox / code exec | `ros/tools/sandbox/base.py` | `CodeExecutor` is **STATELESS** (`run(CodeRunRequest{source,kwargs}) -> CodeRunResult`). Tiers `restricted` (in-proc) / `freestyle` (VM). **Not a persistent workspace.** |
| Execution backend | `ros/execution/{registry,freestyle}.py` | **Per-run**: `local` (master process) or `freestyle` (whole run on a VM). |
| Object storage | `ros/artifacts/{base,store,backends}.py` | **WS7 phases 1–2 already shipped**: `ObjectStore` (local + s3), `BucketResolver`, `ArtifactStore` (content-addressed `{env}/{tenant}/{project}/{run}/{sha}/{file}` key scheme, size cap), an `Artifact` DB row + `artifact:read/write` router. See `docs/design/artifact-storage.md`. |
| Bucket provisioning | `ros/services/providers/railway_storage.py` | Provisions an **S3 bucket per agent** (creds as `secret://` refs + endpoint) — the infra the `s3` tier points at. |
| Governance | `ros/services/apikeys.py`, `ros/authz.py`, `middleware_compiler.py` | Governed-subject `allows()`/`enforce_capacity()`; route perms; `tenant_budget` hard-cap middleware; per-run `egress_policy`. |

**Two consequences that shape the design:**
- The code node needs a **new stateful `Workspace` seam** — `CodeExecutor` can't model a checked-out repo + shell.
- The artifact plane needed only its **graph half** — the store existed (WS7), but nothing connected it to the engine: no state channel, no reducer, no producer/consumer path. That half is slice 1 (shipped; = WS7 phase 3's "refs-in-state").

## 4. Design

### 4.1 Artifact plane (Part A)

**Artifact entry** (what rides the state; small, checkpoint-safe) — a serialized `ArtifactRef`
plus the one piece of graph metadata the plane needs:
```
{ bucket, key, sha256, size, content_type, filename,   # ArtifactRef — the durable pointer
  produced_by }                                        # emitting node id — what consumers filter on
```
Deliberately **no id / timestamp**: the key is already content-addressed, so a replayed emit
reproduces the entry byte-for-byte and the reducer treats it as an update, not a duplicate.

- **`artifacts` state channel** — a `list[json]` channel with an **append/merge-by-`(bucket, key)` reducer** (`state.py:REDUCERS["artifacts"]`), added as a **default channel alongside `messages`** so any node can hand off a file without the author declaring the channel. Parallel branches merge without clobbering; re-emitting updates in place. `langgraph_transpile` mirrors both (exported graphs carry the same channel + reducer).
- **`ArtifactStore`** — **already exists** (`ros/artifacts/store.py`, WS7): `put(...) -> ArtifactRef`, `get`, `delete`, `presign`, `delete_run/_project`, content-addressed keys, `BucketResolver`, size cap. Tiers `local` / `s3` via `ROS_ARTIFACT_STORE`; **deployed default is `s3`** (the provisioned bucket), `local` is the test/dev fake. Nothing new was needed here.
- **`ros/artifacts/state.py`** (new) — the bridge: `to_entry`/`from_entry`, `emit(...)` (put bytes → entry; write-ahead), `load(entry)` (entry → bytes), `select(state, produced_by=…)`, `run_scope(config)` (artifacts key on `configurable.run_id`, now carried by every run invocation).
- **Reachable from the VM** — because the code node's loop runs *on* the workspace VM (§4.2), the store's S3 creds must ride the **`RunManifest` secret refs / `runtime_env`**, not master-only config. The VM writes artifacts directly to the bucket and returns **refs**; bytes never transit the orchestrator.
- **Producer / consumer** — a node returns `{"artifacts": [ref]}`; downstream nodes read `state["artifacts"]` (or select specific refs via existing edge `mappings` JMESPath). No new routing primitive needed for MVP.
- **Checkpoint discipline** — refs in state (checkpointed, tiny); **bytes never in the checkpoint**. GC/TTL sweeps the store on run/thread completion.
- **Later:** declare artifact contracts on edges so the **validator** (`node_io.py` + validator) checks "node A emits `report.pdf`, node B requires it" at build time.

### 4.2 Code node + Workspace seam (Part B)

**`Workspace`** (new stateful protocol, `ros/tools/sandbox/workspace.py`, sibling to `CodeExecutor`):
```
class Workspace(Protocol):
    id: str
    async def checkout(repo_url, ref, *, creds) -> None      # git clone/fetch into the workspace
    async def read(path) / write(path, data) / ls(path)      # filesystem
    async def exec(argv|cmd, *, timeout, cwd) -> ExecResult   # shell (bash, build, test)
    async def diff() -> str                                    # working-tree diff
    async def commit(msg) / push(branch, *, creds) -> str      # -> commit sha
    async def snapshot(paths) -> list[ArtifactRef]             # capture outputs into ArtifactStore
```
- **Tiers** via a `get_workspace()` registry (mirrors `get_code_executor`): **`freestyle` is the shipped default** (a real VM/container — reuses the Freestyle sandbox/execution plumbing); `local` (tmpdir + `asyncio` subprocess) is retained as the **test/dev fake only**.
- **`code` node** — `register(NodeSpec(type="code", category="agents", …))`. Factory reuses the **deep-agent loop** (`ros/nodes/agent_node.py`) but its tools are **workspace-backed** (`read_file`, `write_file`, `edit`, `bash`, `git`). Flow: acquire lease → resolve workspace → `checkout` → agent loop → `snapshot`/`diff`/`commit` → write `{"artifacts":[…], "<output_key>": diff}`.
- **Topology — delegate-the-loop-on-VM** (decided, §5.3). The agent loop runs **on the workspace VM**, so its `read_file`/`write_file`/`edit`/`bash`/`git` tools hit the **local filesystem** with no per-tool RPC round-trip; it writes artifacts to S3 and returns **refs**. Reuses the standalone-runtime plumbing already in the repo — `ros/runtime/runner.py` (manifest→graph), `ros/runtime/driver.py` (trusted-VM streaming dispatch), `ros/execution/freestyle.py`. The orchestrator side is therefore a **session handle**, not a command channel: acquire lease → ensure VM + checkout → dispatch → stream/await → collect refs. (The rejected alternative, *remote-drive*, kept the loop in the orchestrator and shipped every tool call as an RPC.)
- **Repo creds** — a git token as `secret://`, resolved by `auth_resolver`, injected into the workspace **only**, never into prompt/state. **Egress allow-list** (github + package registries) via `egress_policy`.
- **Output** — artifacts (diff, branch, commit sha, build logs); optionally open a PR (git push + a REST/MCP tool).

### 4.3 Composition — the run workspace (Part C)

- The workspace is **addressable** and **long-lived per `(agent, end_user)`** (decided, §5.2) — a **provisioned resource** (`provision_resource`, alongside `railway-storage`/compute) with its own record: workspace id, VM id, repo/ref, `last_used_at`, TTL. Multiple nodes in a run — and later runs for the same end user — **share one checkout** (upstream edits visible downstream, warm deps, no re-clone).
- **Concurrency — Redis lease, serialized** (decided). A long-lived checkout is shared mutable state, so one run holds it at a time: acquire lease on `(agent, end_user)` → heartbeat while the node runs → release on completion. Reuses the **existing Redis-lease/reclaim pattern** (stale lease from a dead run is reclaimed on TTL expiry). A second run **waits or fails fast** — surfaced as a node-level busy state, not a silent queue.
- **GC/TTL** — idle workspaces are swept (VM torn down, artifacts already durable in S3); the checkout is a cache, never the source of truth.
- **Graph runs stay ephemeral**; nodes exchange only **refs + commit sha**. On resume, replay from those, **do not re-run the build** (§4.5).
- Mental model: **one workflow graph (one execution locus) + a code node that drives an isolated workspace off to the side** — not "the workflow splits onto another VM."

### 4.4 Governance

- New capability **`code:execute`** (and possibly **`repo:write`**) in `authz.py:PERMISSIONS` + the governed-subject allow-list (`ApiKeyService.allows`), gated exactly like `backend:provision`.
- **Resource caps** on the workspace (walltime/CPU/mem — the `CodeRunRequest` limits already model this) + per-subject `enforce_capacity`; `tenant_budget` hard-cap already applies per node.
- **Egress** enforced on the workspace's network; **feature gate** `ROS_ENABLE_CODE_NODE` (like `ROS_ENABLE_CODE_TOOLS`).

### 4.5 Replay / durability / HITL

- Checkpoint at node boundaries; the **artifact refs + commit sha are the durable handoff**. Content-addressing makes a re-emitted artifact a no-op.
- **Long builds** — the node awaits the workspace; for long sessions support **interrupt/HITL** (pause → human review → resume) via the existing `human_input`/interrupt patterns rather than a single multi-hour await.
- Ephemeral counters use `UntrackedValue` (already the pattern for run-scoped tallies) so they don't bloat the checkpoint.

## 5. Decisions (settled 2026-08-19)

| # | Decision | Chosen | Note |
|---|---|---|---|
| 1 | Storage backend | **`s3` (provisioned bucket) from the start** | `local` tier survives as a test fake only. Creds must reach the VM via manifest/`runtime_env` (§4.1). |
| 2 | Workspace lifecycle | **Long-lived per `(agent, end_user)`** | Provisioned resource with TTL/GC; checkout is a cache, S3 is the durable store (§4.3). |
| 3 | Code-node topology | **Delegate-the-loop-on-VM** | Loop runs on the workspace VM over the local FS; orchestrator holds a session handle. Reuses `ros/runtime/{runner,driver}.py` (§4.2). |
| 4 | Infra timing | **Both now** — bucket **and** Freestyle VM wired from slice 1 | No local-only push; every slice is **LIVE-VERIFY**. Local/in-memory fakes are kept purely so CI can run without infra. |
| 5 | Capability naming | **`code:execute`** | Single capability; no separate `repo:write` for now — repo write is covered by `code:execute` + the git token's own scope. |
| 6 | Driving use case | **Generic "agent works on a git repo"** | Tool set and egress allow-list stay broad (github + common package registries); narrow them when a concrete target repo appears. |
| 7 | Shared-workspace concurrency *(new — raised by #2)* | **Redis lease, serialized** | One run per `(agent, end_user)` checkout; heartbeat + TTL reclaim, reusing the run-reclaim lease pattern (§4.3). |

**Consequence of #4:** the original "slices 1–4 need no external infra" property is gone. Sequencing below is therefore ordered by **dependency and risk**, not by infra-free-ness — and the `code:execute` gate moves *earlier* (into the node slice), since a live VM with repo write is ungated otherwise.

**Open bug surfaced by slice 1's live-verify** — `railway-storage` records the bucket's **display
name**, but the addressable S3 bucket name differs (`ros-artifacts` vs `ros-artifacts-eaduobyfsc`;
PutObject to the former returns `NoSuchBucket`), and the credentials query drops `region`/`urlStyle`.
Any agent provisioned through that provider therefore gets an unusable storage config. Blocks the
"creds reach the VM via `runtime_env`" step in §4.1, so it must be fixed before slice 3. Needs a
`RAILWAY_API_TOKEN` to confirm the GraphQL field names. See the module docstring.

## 6. Delivery plan (independently shippable slices)

| # | Slice | Infra | Acceptance |
|---|---|---|---|
| **1** ✅ | **Artifact plane** — `artifacts` channel + merge-by-`(bucket,key)` reducer (`engine/state.py`, mirrored in `langgraph_transpile`), `ros/artifacts/state.py` bridge (`emit`/`load`/`select`/`to_entry`/`from_entry`/`run_scope`), `run_id` on every run's `configurable`. Store reused as-is from WS7 | bucket | **Done + LIVE-VERIFIED.** `tests/test_artifact_plane.py` (18, offline): producer→consumer handoff through a compiled graph, parallel-branch merge, in-place update on re-emit, bytes absent from the checkpoint, JMESPath edge selection. `tests/test_artifact_plane_live.py` (4, opt-in) against the real Railway bucket: key scheme as stored, object present off the wire, presigned GET downloads unauthenticated, content-addressed overwrite + `delete_run` reclaim |
| **2** | **Workspace resource + lease** — workspace record as a provisioned resource keyed by `(agent, end_user)`; VM create/attach/GC; Redis lease acquire/heartbeat/release | VM | two concurrent runs **serialize** on one workspace; lease survives a master restart; stale lease reclaimed on TTL; idle workspace swept |
| **3** | **`Workspace` seam + `FreestyleWorkspace`** — protocol + `get_workspace()` registry + VM impl (`checkout`/`read`/`write`/`ls`/`exec`/`diff`/`commit`/`push`/`snapshot`); git token as `secret://`; egress allow-list | VM | clone a real repo on the VM, edit, run a build, `diff`, `snapshot` → artifact in S3; token never in prompt/state; egress denial verified |
| **4** | **`code` node (delegate-on-VM)** — `NodeSpec` + factory; dispatches the deep-agent loop onto the VM via the runner/driver path; collects refs. Ships with the **`code:execute` capability + `ROS_ENABLE_CODE_NODE` gate** (pulled forward from old slice 4) | VM | node clones repo, edits, returns a diff artifact; downstream node consumes it; **denied without `code:execute` / when the gate is off**; optional: PR opened |
| **5** | **Governance hardening** — walltime/CPU/mem caps, `enforce_capacity` per subject, `tenant_budget` interaction, egress policy on the workspace network | VM | walltime cap kills a runaway build; capacity + budget denials tested; egress allow-list enforced under load |
| **6** | **Cross-node + cross-run polish** — edge artifact contracts + validator check; prove workspace reuse across runs; artifact GC/TTL sweep | VM | validator flags "node B requires an artifact node A doesn't emit"; run 2 sees run 1's edits without re-cloning; TTL sweep reclaims |
| **7** | **HITL long builds** — interrupt/resume around a multi-minute build via the existing `human_input`/interrupt path | VM | pause → human review of a diff → resume, without re-running the build |

**Sequencing rationale:** 1 is the true foundation (the code node's handoff format) and the only slice that stands alone. 2 before 3 because a long-lived workspace's *identity and lease* must exist before anything mutates it. 3 before 4 because the node is a consumer of the seam. The capability gate rides with 4 rather than trailing it, since that is the first slice where a live VM gets repo write.

Every slice is **LIVE-VERIFY** (decision §5.4). Local/in-memory fakes exist only so the test suite runs in CI without a bucket or VM — they are not an acceptance path.

## 7. Risks

- **Checkpoint bloat** — mitigated by refs-not-blobs; enforce in review (nothing binary in state channels).
- **Security** — real shell + repo write is the highest-risk surface: isolation lives at the **workspace boundary** (Freestyle/container), egress allow-list, capability + budget, secret scoping (git token as `secret://`, never in prompt/state). RestrictedPython is **not** sufficient here.
- **Non-determinism / replay** — builds aren't deterministic; replay from commit sha + artifact refs, never by re-running.
- **Infra dependency** — *every* slice needs Freestyle + a bucket + git creds (§5.4); treat like the provider `LIVE-VERIFY` warnings. Loss of the infra-free first push is the accepted cost of shipping the real architecture directly.
- **Cost / latency** — VM spin-up; warm-VM pooling already exists (`freestyle_warm_vms`). Long-lived workspaces cut clone cost but hold VMs open — TTL/GC tuning is a live cost lever.
- **Lease liveness** *(new, from §5.7)* — a crashed run must not wedge an `(agent, end_user)` workspace forever; TTL reclaim is the safety net and needs an explicit test. Conversely, reclaiming too eagerly risks two writers on one checkout.
- **Shared-checkout drift** *(new, from §5.2)* — a long-lived workspace accumulates state (dirty tree, stale branch, build cache) across runs, so a run's outcome depends on its predecessors. Mitigate with a per-run clean/reset step and treat the checkout as a cache, never as durable state.
