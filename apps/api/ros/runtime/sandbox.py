"""Sandbox run driver — the ISOLATING data-plane path (WS10 Phase 1).

Runs a whole workflow inside a per-run sandbox that holds NO ambient authority: no master DB/Redis
handle, no master key, no other tenant's secrets. It reaches master ONLY over the run-scoped token:
  1. PULL the RunManifest  (GET  /v1/runtime/runs/{id}/manifest) → rebuild a CompileContext DB-less.
  2. STREAM the graph, batching SSE frames to master (POST .../frames) so the browser still sees live
     node progress; heartbeat via (POST .../status).
  3. FINALIZE by posting the terminal status/answer/tokens/cost (POST .../result).

Contrast `ros/runtime/driver.py` (trusted-VM): that connects to the shared Postgres/Redis directly.
This one deliberately does not — that is the whole isolation boundary. See
docs/design/sandbox-backend-build-plan.md.

Scope (P1a): NON-HITL runs only. HITL/resume needs the callback-backed checkpointer (G-C, P1b); a run
that raises an interrupt here finalizes as `error` with a clear message. Uses an in-process checkpointer
(the sandbox is one run) — no DB durability, by design, until G-C lands.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("ros.runtime.sandbox")

# How many stream frames to batch before flushing to master (bounds callback chattiness while keeping
# the browser near-live). Also flushed on a time budget so a slow node still heartbeats.
_FRAME_BATCH = 8
_HEARTBEAT_EVERY_S = 15.0


async def drive_sandbox(
    *,
    master_url: str,
    token: str,
    run_id: str,
    input: dict,
    public: bool = False,
    run_context: dict | None = None,
) -> int:
    """Pull the manifest for `run_id`, drive the workflow, stream frames + finalize via master
    callbacks. Returns a process exit code (0 ok, 1 run errored, 2 setup failure)."""
    from ros.runtime.client import MasterCallback, fetch_manifest
    from ros.runtime.env import apply_runtime_env
    from ros.services.run_frames import map_chunk_frames
    from ros.services.runs import _recursion_limit

    cb = MasterCallback(master_url, token, run_id)
    try:
        try:
            manifest = await fetch_manifest(master_url, token, run_id)
        except Exception as e:  # noqa: BLE001 - can't run without the manifest
            log.exception("sandbox: manifest pull failed for %s", run_id)
            await _safe_result(cb, status="error", error=f"manifest pull failed: {e}")
            return 2

        # 2b: provisioned per-(agent,end_user) resource env, precomputed in the manifest. VM-only.
        apply_runtime_env(manifest.get("runtime_env"))

        executable = manifest.get("executable") or {}
        wf_nodes = executable.get("nodes", [])
        node_ids = {n.get("id") for n in wf_nodes if isinstance(n, dict)}
        from ros.services.runs import _internal_message_nodes

        suppressed = _internal_message_nodes(wf_nodes)

        # In-process tracer for token/cost totals (posted in /result) + an in-memory checkpointer
        # (non-HITL). NO shared-Postgres saver here — that is the isolation boundary.
        from langgraph.checkpoint.memory import InMemorySaver

        from ros.services.runtime import build_compile_context_from_manifest
        from ros.tracing.tracer import ROSTracer

        tracer = ROSTracer()
        end_user = None  # end-user identity travels inside runtime_env / run_context, not needed here
        ctx = build_compile_context_from_manifest(
            manifest, checkpointer=InMemorySaver(),
            end_user=end_user, run_context=run_context,
        )
        from ros.engine.compiler import compile_workflow

        try:
            graph = compile_workflow(executable, ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("sandbox: compile failed for %s", run_id)
            await _safe_result(cb, status="error", error=f"compile error: {e}")
            return 1

        config: dict[str, Any] = {
            "configurable": {"thread_id": run_id, "run_id": run_id},
            "callbacks": [tracer],
            "recursion_limit": _recursion_limit(executable),
        }

        await cb.status("running")
        seq = 0
        pending: list[dict] = []
        last_flush = time.monotonic()

        async def _flush(force: bool = False) -> None:
            nonlocal pending, last_flush
            if pending and (force or len(pending) >= _FRAME_BATCH):
                batch, pending = pending, []
                await cb.frames(batch)
                last_flush = time.monotonic()

        # opening frame (mirrors _drive's "run" frame so the browser binds the thread)
        seq += 1
        pending.append({"seq": seq, "event": "run", "data": {"run_id": run_id, "thread_id": run_id}})

        interrupted = False
        try:
            async for ns, mode, chunk in graph.astream(
                input, config,
                stream_mode=["tasks", "updates", "messages", "custom"],
                subgraphs=True,
            ):
                for frame in map_chunk_frames(
                    ns, mode, chunk, public=public,
                    node_ids=node_ids, suppressed_message_nodes=suppressed,
                ):
                    seq += 1
                    pending.append({"seq": seq, "event": frame["event"], "data": frame["data"]})
                await _flush()
                if time.monotonic() - last_flush > _HEARTBEAT_EVERY_S:
                    await _flush(force=True)
                    await cb.status("running")
        except Exception as e:  # noqa: BLE001 - a run error is a normal terminal outcome
            # A HITL interrupt surfaces here as GraphInterrupt in P1a (no checkpoint proxy yet).
            if type(e).__name__ in ("GraphInterrupt", "Interrupt"):
                interrupted = True
                log.info("sandbox: run %s hit an interrupt (HITL not supported in P1a)", run_id)
                await _flush(force=True)
                await _safe_result(
                    cb, status="error",
                    error="HITL (human_input/handoff) is not yet supported on the sandbox backend",
                )
                return 1
            log.exception("sandbox: run %s errored", run_id)
            seq += 1
            pending.append({"seq": seq, "event": "error", "data": {"message": _redact(public, str(e))}})
            await _flush(force=True)
            tokens, cost = tracer.totals()
            await _safe_result(cb, status="error", error=str(e), total_tokens=tokens, total_cost_usd=cost)
            return 1

        # Terminal snapshot: final graph state for the answer + token/cost totals.
        final_state = await graph.aget_state(config)
        values = getattr(final_state, "values", {}) or {}
        seq += 1
        pending.append({"seq": seq, "event": "done", "data": {"run_id": run_id}})
        await _flush(force=True)

        tokens, cost = tracer.totals()
        from ros.util.serialize import jsonable

        output = {}
        try:
            output = jsonable(values)
        except Exception:  # noqa: BLE001 - never strand the run on a non-serializable state
            log.exception("sandbox: could not serialize output for %s", run_id)
        await _safe_result(cb, status="done", output=output, total_tokens=tokens, total_cost_usd=cost)
        log.info("sandbox drove run %s to done (interrupted=%s)", run_id, interrupted)
        return 0
    finally:
        await cb.aclose()


def _redact(public: bool, detail: str) -> str:
    return "Something went wrong while processing your request. Please try again." if public else detail


async def _safe_result(cb, **kw) -> None:
    """Post /result, tolerating a transient callback failure (logged; the reaper is the backstop)."""
    try:
        await cb.result(**kw)
    except Exception:  # noqa: BLE001
        log.exception("sandbox: result callback failed (%s)", kw.get("status"))
