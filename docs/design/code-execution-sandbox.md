# Design — code-tool execution sandbox (WS5 · 5a)

**Status:** Design
**Problem:** code tools run user-authored Python. Today (`ros/tools/code.py`) that's
**RestrictedPython only** — AST hardening with **no CPU/memory/wall-clock bound and no OS
isolation**. A runaway (`while True: pass`) or memory hog can't be killed and ties up a shared
thread; the process shares the api's kernel, filesystem, network, and **env (which now holds
`ROS_SECRET_KEY`, DB creds)**. Safe today only because code tools are off by default and the prod
guard blocks enabling them without an explicit `allow_unsandboxed_code_tools` acknowledgement.
Goal: make code tools **safe to enable for untrusted / multi-tenant** users.

## 1. Goals / non-goals

**Goals**
- Hard, enforced per-execution limits: wall-clock, CPU, memory, output size, no fork-bomb.
- A runaway is **actually killed** (fix the documented thread-DoS residual).
- No ambient authority: no api env/secrets, no DB/Redis, no filesystem writes, network default-deny.
- Pluggable isolation tiers (in-process → subprocess → container/microVM) behind one seam, so the
  MIT core stays dependency-light and stronger tiers ship as plugins.
- Backward compatible: default behaviour unchanged; safe-by-default preserved.

**Non-goals**
- Not redesigning the deep-agent **filesystem** sandbox (`ctx.sandbox_backend_for`, `agent_node.py`)
  — that's a state/FS backend, a different seam. (Convergence noted in §7.)
- Not sandboxing MCP **stdio** transport here (separate arbitrary-exec gap; §7).

## 2. The seam

One chokepoint already exists — `execute_code(cfg, kwargs)` (async), reached only via
`build_code_tool` (callers: `tools/materialize.py`, `services/tools.py`). Introduce a
`CodeExecutor` interface and make `execute_code` a thin adapter over it, mirroring the existing
`ExecutionBackend` pattern (`ros.execution_backends` entry-point + lazy `get_backend()`).

```python
# ros/tools/sandbox/base.py
@dataclass(frozen=True)
class CodeRunRequest:
    source: str
    language: str                    # "python"
    kwargs: dict[str, Any]           # tool-call args from the LLM (the ONLY data crossing in)
    limits: ResourceLimits           # wall_s, cpu_s, mem_mb, output_max_chars, nproc, fsize=0
    allowed_imports: frozenset[str]  # stdlib allowlist (may widen once isolated)
    egress: EgressPolicy             # default DENY; else via the SSRF guard
    labels: dict[str, str]           # tenant_id / project_id / run_id — accounting only, not authority

@dataclass(frozen=True)
class CodeRunResult:
    ok: bool
    result: Any | None               # JSON-serializable, already size-capped
    error: str | None                # "compile" | "runtime:<Type>" | "killed:cpu|mem|wall|output"
    metrics: dict                     # wall_ms, cpu_ms, peak_rss_mb, killed_reason

class CodeExecutor(Protocol):
    async def run(self, req: CodeRunRequest) -> CodeRunResult: ...
```

Selection: `ROS_CODE_EXECUTOR` (default `restricted`) → `get_code_executor()` resolves `restricted`
/ `subprocess` built-in, else a `ros.code_executors` entry-point plugin (lazy import — core never
imports a tier it doesn't use). `execute_code` builds the request from `cfg`/settings and returns
`result` or raises `CodeToolError` mapped from `CodeRunResult.error`.

## 3. Executor tiers

| Tier | Isolation | Kills runaway? | Blocks network | Blocks FS/secret access | Infra | Use for |
|---|---|---|---|---|---|---|
| **`restricted`** (default, MIT) | AST only (in-process) | ❌ (thread can't die) | ❌ | partial (allowlist) | none | trusted single-tenant (today) |
| **`subprocess`** (MIT) | OS process + `rlimit` | ✅ SIGKILL process-group | ⚠️ policy-only¹ | ✅ (scrubbed env, `RLIMIT_FSIZE=0`) | none | self-host, low-trust |
| **`container` / `gvisor` / microVM** (plugin) | kernel/namespace/VM | ✅ cgroup kill | ✅ netns + egress proxy | ✅ ephemeral ro-rootfs | Docker/Podman/Firecracker | hostile multi-tenant (hosted) |

¹ A bare subprocess can't drop network without namespaces/root; it enforces network-deny only by
not handing out clients + (optionally) a `no_proxy`/blocked-socket shim. True egress control needs
the container tier's network namespace + SSRF-enforcing proxy.

### 3a. `subprocess` tier (the recommended baseline — fixes the DoS)
- Spawn a fresh `python -I -S` child running a tiny bootstrap; **setsid** (own process group).
- In `preexec_fn` (child, pre-exec): `resource.setrlimit` for `RLIMIT_CPU` (cpu_s),
  `RLIMIT_AS`/`RLIMIT_DATA` (mem_mb), `RLIMIT_NOFILE`, `RLIMIT_NPROC` (no fork-bomb),
  `RLIMIT_FSIZE=0` (no writes); drop to a non-root uid if available.
- **Scrub the environment** — pass an empty/whitelisted env; the child must NOT inherit the api's
  `ROS_*`/DB/secret env. (Critical now that `ROS_SECRET_KEY` lives in env.)
- IO: source + `kwargs` in via a pipe as JSON; result out as JSON; still run RestrictedPython inside
  as defence-in-depth.
- Parent: `asyncio.wait_for(wall_s)`; on timeout `os.killpg(SIGKILL)` — the runaway actually dies.
- Bound total concurrent sandboxes (semaphore / per-tenant quota) so N tools can't fork-storm.
- Caveat: shares the host kernel/FS namespace → bounds **DoS + accidents**, not a kernel-exploit
  escape. For hostile code go to the container tier.

### 3b. `container` / microVM tier (plugin, strongest)
Ephemeral container (or gVisor/Firecracker) per exec: read-only rootfs, tmpfs `/tmp` with quota,
**no network** (or egress via a proxy that reuses the SSRF policy), cgroup CPU/mem, seccomp +
non-root, killed at limits. Optionally a warm pool for latency, or delegate to an external
code-interpreter/sandbox service (E2B/Daytona-style). Ships behind `ros.code_executors` — not in
the MIT core (keeps the core free of a container dependency), like the cloud execution backend.

## 4. Cross-cutting requirements (all tiers)
- **No ambient authority:** child gets only `kwargs`. Never inject `{{ctx.*}}` secrets or api env.
- **Limits are settings:** `ROS_CODE_TOOL_TIMEOUT_S`, `_CPU_S`, `_MEM_MB`, `_MAX_RESULT_CHARS`
  (promote the current `_MAX_RESULT_CHARS` module constant), `_MAX_CONCURRENCY`.
- **Egress:** default deny; if a tool needs HTTP it should be a REST tool (already SSRF-guarded),
  not arbitrary sockets in a code tool.
- **Observability:** put `metrics` (wall/cpu/rss/killed_reason) on the tool trace span; audit each
  code-tool execution (actor, tenant, killed?).
- **Clean failure mapping:** `killed:*` → `CodeToolError("resource limit exceeded: <reason>")` so
  the agent gets a clear, non-crashing tool error.

## 5. Prod-guard change
Today: `enable_code_tools=True` in prod requires `allow_unsandboxed_code_tools=True`.
New: that acknowledgement is required **only for the `restricted` tier**. With an **isolating**
executor (`subprocess`/`container`) selected, code tools may be enabled in production **without**
the unsandboxed flag — the guard checks `ROS_CODE_EXECUTOR ∈ {isolating}` instead.

## 6. Rollout
1. **Seam + refactor** — add `CodeExecutor`, `get_code_executor()`, config; `execute_code` becomes
   an adapter; default `restricted` (zero behaviour change). Tests + `test_no_cloud_imports` parity.
2. **`subprocess` executor** (MIT) — rlimits + `killpg` + env scrub + `RLIMIT_FSIZE=0` + concurrency
   cap. Security tests: runaway CPU killed, mem cap hit, fork-bomb blocked, env not leaked, no FS write.
3. **Guard relax** — allow code tools in prod under an isolating executor (§5).
4. **`container`/microVM plugin** — ephemeral sandbox + egress proxy + pooling, behind
   `ros.code_executors`.

## 7. Related / open
- **Deep-agent FS sandbox** (`ctx.sandbox_backend_for`) is a separate seam; a future unified
  "sandbox provider" could supply both a filesystem and a code-exec env (one external sandbox
  service). Keep separate for now.
- **MCP stdio** transport (`enable_mcp_stdio`) is a sibling arbitrary-exec gap — should stay off or
  route through comparable isolation; track separately.
- **Latency:** subprocess spawn ~10–50ms; container cold-start 100ms–seconds → warm pool if code
  tools become hot-path.
- **Determinism:** sandbox has no clock/network guarantees; document that code tools aren't
  reproducible across runs.
