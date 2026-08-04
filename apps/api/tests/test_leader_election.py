"""A/C2 - distributed scheduler leader election / failover.

A/C12 delivered the leader-gated loops + the Redis lease; A/C2 adds the Postgres advisory-lock
backend so a Redis-less multi-replica prod gets real election (no static-flag SPOF). The actual
Redis / Postgres locks need those services, so CI covers the backend SELECTION + graceful
fallback; the live lock/failover is a manual/integration check.
"""

from __future__ import annotations

from ros.config import settings
from ros.execution.local import LocalBackend, _db_is_postgres


def test_db_is_postgres_detection(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///x.db")
    assert _db_is_postgres() is False
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u@h/db")
    assert _db_is_postgres() is True
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u@h/db")
    assert _db_is_postgres() is True


async def test_singleton_sqlite_dev_uses_flag(monkeypatch):
    # No Redis + SQLite (dev) -> the static scheduler_leader flag, unchanged single-process behavior.
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///x.db")
    b = LocalBackend()

    monkeypatch.setattr(settings, "scheduler_leader", True)
    async with b.singleton("reaper") as leader:
        assert leader is True

    monkeypatch.setattr(settings, "scheduler_leader", False)
    async with b.singleton("reaper") as leader:
        assert leader is False


async def test_singleton_pg_branch_falls_back_when_lock_unavailable(monkeypatch):
    # database_url LOOKS like Postgres so the PG advisory branch is taken, but the bound engine is
    # SQLite (no pg_try_advisory_lock) -> the branch errors and gracefully falls back to the flag.
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u@h/db")
    monkeypatch.setattr(settings, "scheduler_leader", True)
    b = LocalBackend()
    async with b.singleton("reaper") as leader:
        assert leader is True  # fell back to the flag rather than crashing the sweep
