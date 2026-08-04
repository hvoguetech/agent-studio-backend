"""LocalBackend (A/C12, Doc §3.3) - the built-in MIT backend.

Wraps TODAY's behavior behind the seam with NO behavior change: inline exec or arq offload,
the in-process scheduler tick, and the stale-run reaper. Crash-reclaim is extended by A/C9
(#23); operator `retry` is delivered by A/C11 (#25). Nothing here imports cloud/SSPL code.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from ros.config import settings
from ros.execution.base import ExecutionBackend

log = logging.getLogger("ros.execution.local")


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
        from ros.services.runs import RunService

        state = getattr(self._app, "state", None)
        return RunService(
            checkpointer=getattr(state, "checkpointer", None),
            store=getattr(state, "store", None),
        )

    async def submit(self, *, run_id, tenant_id, project_id=None, run_service=None) -> dict:
        # Offload to the arq worker when configured; else run inline (the pre-seam dispatch path).
        from ros.queue import enqueue_run

        if await enqueue_run(run_id, tenant_id, project_id):
            return {"run_id": run_id, "status": "queued", "queued": True}
        rs = self._run_service(run_service)
        return await rs.run_to_completion(
            run_id=run_id, tenant_id=tenant_id, project_id=project_id
        )

    async def retry(self, *, run_id, tenant_id, mode, project_id=None, run_service=None) -> dict:
        rs = self._run_service(run_service)
        if mode == "resume":
            return await rs.retry_resume(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
        if mode == "restart":
            # Create a fresh run on the current published version; the caller drives it (streams).
            return await rs.create_retry_run(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
        raise ValueError(f"unknown retry mode: {mode!r} (expected 'resume' or 'restart')")

    async def reclaim_orphans(self) -> int:
        # A/C9: first RE-DRIVE fresh orphans (crashed drivers) from their checkpoint, then run the
        # stale-run reaper as the backstop (expired queued runs, HITL timeouts, too-old runs).
        rs = self._run_service()
        reclaimed = await rs.reclaim_running_orphans()
        reaped = await rs.reap_stale_runs()
        return reclaimed + reaped

    async def run_scheduler_tick(self) -> int:
        from ros.services.dispatch import run_due_app_events, run_due_schedules

        rs = self._run_service()
        fired = await run_due_schedules(rs)
        fired += await run_due_app_events(rs)
        return fired

    def singleton(self, name: str, *, ttl_seconds: int = 120):
        return _local_singleton(name, ttl_seconds)


@asynccontextmanager
async def _local_singleton(name: str, ttl_seconds: int):
    """Leader/singleton gate with automatic failover (A/C2). Precedence:

    1. Redis `SET NX EX` - a cross-replica lease (leadership expires on the TTL if the holder dies).
    2. Postgres SESSION advisory lock - REAL election without Redis (Postgres is the mandatory prod
       DB); the lock auto-releases when the holder's connection drops, so leadership fails over.
    3. The static `scheduler_leader` flag - SQLite dev / single process only.

    Yields True to exactly one holder, False to everyone else. Replaces the old static-flag SPOF."""
    if settings.redis_url:
        async with _redis_singleton(name, ttl_seconds) as leader:
            yield leader
        return
    if _db_is_postgres():
        async with _pg_advisory_singleton(name) as leader:
            yield leader
        return
    yield bool(settings.scheduler_leader)


def _db_is_postgres() -> bool:
    url = (settings.database_url or "").lower()
    return "postgres" in url or "+asyncpg" in url or "+psycopg" in url


@asynccontextmanager
async def _redis_singleton(name: str, ttl_seconds: int):
    """Cross-replica lease via Redis `SET NX EX`; the lease TTL is the failover window."""
    import uuid

    key = f"ros:singleton:{name}"
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


@asynccontextmanager
async def _pg_advisory_singleton(name: str):
    """Leader election via a Postgres SESSION-level advisory lock, held on a dedicated connection
    for the tick's duration. On the holder's death the connection drops and Postgres releases the
    lock, so another replica acquires it on its next tick (automatic failover). NOTE: needs a
    session-pinned connection - NOT compatible with PgBouncer transaction pooling."""
    import hashlib

    from sqlalchemy import text

    from ros.db.base import SessionLocal

    key = int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True)
    async with SessionLocal() as session:
        try:
            got = bool(
                (await session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
            )
        except Exception:  # noqa: BLE001 - a DB blip must not stop the sweep; fall back to the flag
            log.debug("singleton(%s): pg advisory lock unavailable; using leader flag", name, exc_info=True)
            yield bool(settings.scheduler_leader)
            return
        try:
            yield got
        finally:
            if got:
                try:
                    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                except Exception:  # noqa: BLE001
                    pass
