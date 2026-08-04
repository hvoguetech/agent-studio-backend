"""LocalBackend (A/C12, Doc §3.3) - the built-in MIT backend.

Wraps TODAY's behavior behind the seam with NO behavior change: inline exec or arq offload,
the in-process scheduler tick, and the stale-run reaper. Crash-reclaim is extended by A/C9
(#23); operator `retry` is delivered by A/C11 (#25). Nothing here imports cloud/SSPL code.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from forge.config import settings
from forge.execution.base import ExecutionBackend

log = logging.getLogger("forge.execution.local")


class LocalBackend(ExecutionBackend):
    name = "local"

    def __init__(self) -> None:
        self._app: Any = None

    async def startup(self, app: Any) -> None:
        self._app = app

    def _run_service(self, override: Any = None):
        """The RunService to drive with. Prefer a caller-provided one (keeps its checkpointer);
        otherwise build one bound to the app's checkpointer/store."""
        if override is not None:
            return override
        from forge.services.runs import RunService

        state = getattr(self._app, "state", None)
        return RunService(
            checkpointer=getattr(state, "checkpointer", None),
            store=getattr(state, "store", None),
        )

    async def submit(self, *, run_id, tenant_id, project_id=None, run_service=None) -> dict:
        # Offload to the arq worker when configured; else run inline (the pre-seam dispatch path).
        from forge.queue import enqueue_run

        if await enqueue_run(run_id, tenant_id, project_id):
            return {"run_id": run_id, "status": "queued", "queued": True}
        rs = self._run_service(run_service)
        return await rs.run_to_completion(
            run_id=run_id, tenant_id=tenant_id, project_id=project_id
        )

    async def retry(self, *, run_id, tenant_id, mode, project_id=None, run_service=None) -> dict:
        rs = self._run_service(run_service)
        if mode == "resume":
            return await rs._continue_from_checkpoint(
                run_id=run_id, tenant_id=tenant_id, project_id=project_id
            )
        # "restart" (fresh run on the latest published version) is delivered by A/C11 (#25).
        raise NotImplementedError("retry mode 'restart' lands in A/C11 (#25)")

    async def reclaim_orphans(self) -> int:
        # A/C9: first RE-DRIVE fresh orphans (crashed drivers) from their checkpoint, then run the
        # stale-run reaper as the backstop (expired queued runs, HITL timeouts, too-old runs).
        rs = self._run_service()
        reclaimed = await rs.reclaim_running_orphans()
        reaped = await rs.reap_stale_runs()
        return reclaimed + reaped

    async def run_scheduler_tick(self) -> int:
        from forge.services.dispatch import run_due_app_events, run_due_schedules

        rs = self._run_service()
        fired = await run_due_schedules(rs)
        fired += await run_due_app_events(rs)
        return fired

    def singleton(self, name: str, *, ttl_seconds: int = 120):
        return _local_singleton(name, ttl_seconds)


@asynccontextmanager
async def _local_singleton(name: str, ttl_seconds: int):
    """Leader/singleton gate. With Redis: `SET NX EX` - a real cross-replica lease. Without Redis:
    fall back to the static `scheduler_leader` flag, preserving pre-seam single-process behavior.
    Yields True to the holder, False to everyone else. A/C2 (#4) builds real leader-election here."""
    if not settings.redis_url:
        yield bool(settings.scheduler_leader)
        return

    import uuid

    key = f"forge:singleton:{name}"
    token = uuid.uuid4().hex
    redis = None
    acquired = False
    try:
        from redis.asyncio import from_url  # provided by the [workers] extra (arq -> redis)

        redis = from_url(settings.redis_url)
        acquired = bool(await redis.set(key, token, nx=True, ex=ttl_seconds))
        yield acquired
    except Exception:  # noqa: BLE001 - a redis blip must not stop the sweep; fall back to the flag
        log.debug("singleton(%s): redis unavailable; using leader flag", name, exc_info=True)
        yield bool(settings.scheduler_leader)
    finally:
        if redis is not None:
            if acquired:  # release only if we still own the lease (best-effort compare-and-del)
                try:
                    cur = await redis.get(key)
                    cur = cur.decode() if isinstance(cur, bytes) else cur
                    if cur == token:
                        await redis.delete(key)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001
                pass
