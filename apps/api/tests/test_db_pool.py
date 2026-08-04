"""A/C6 - DB connection-pool tuning. SQLite keeps NullPool (dev/test event-loop safety); Postgres
gets a tuned QueuePool (pool_size / max_overflow / timeout / recycle / pre_ping) so many replicas
don't exhaust connections. The live pool needs Postgres; CI covers the kwargs computation."""

from __future__ import annotations

from sqlalchemy.pool import NullPool

from ros.config import settings
from ros.db.base import _pool_kwargs


def test_sqlite_uses_nullpool():
    assert _pool_kwargs("sqlite+aiosqlite:///x.db") == {"poolclass": NullPool}


def test_postgres_pool_is_tuned(monkeypatch):
    monkeypatch.setattr(settings, "db_pool_size", 7)
    monkeypatch.setattr(settings, "db_max_overflow", 13)
    monkeypatch.setattr(settings, "db_pool_recycle", 900)
    monkeypatch.setattr(settings, "db_pool_pre_ping", True)
    kw = _pool_kwargs("postgresql+asyncpg://u@h/db")
    assert "poolclass" not in kw  # a real (Queue) pool, not NullPool
    assert kw["pool_size"] == 7
    assert kw["max_overflow"] == 13
    assert kw["pool_recycle"] == 900
    assert kw["pool_pre_ping"] is True
    assert kw["pool_timeout"] == settings.db_pool_timeout
