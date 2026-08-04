"""A/C12 - the pluggable ExecutionBackend seam.

Covers: the interface contract (AC-1), registry resolution + lazy import (AC-2), the singleton
gate fallback (AC-4), and the shared checkpoint-continue primitive not re-executing completed
work (AC-5). The CI import-guard (AC-7) lives in test_no_cloud_imports.py; behavior parity
(AC-3) is the existing dispatch/scheduler/reaper suites passing unchanged.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from ros.config import settings
from ros.db.base import SessionLocal
from ros.execution import registry
from ros.execution.base import ExecutionBackend
from ros.execution.local import LocalBackend
from ros.models import Run, Workflow
from ros.services.runs import RunService

# --- AC-1: interface contract ------------------------------------------------------------

def test_localbackend_satisfies_interface():
    b = LocalBackend()
    assert isinstance(b, ExecutionBackend)
    assert b.name == "local"


def test_incomplete_backend_cannot_instantiate():
    class Partial(ExecutionBackend):
        async def submit(self, **kw):
            return {}
        # deliberately missing retry / reclaim_orphans / run_scheduler_tick / singleton

    with pytest.raises(TypeError):
        Partial()


def test_test_double_satisfies_interface():
    class Dummy(ExecutionBackend):
        name = "dummy"

        async def submit(self, **kw):
            return {"ok": True}

        async def retry(self, **kw):
            return {"ok": True}

        async def reclaim_orphans(self):
            return 0

        async def run_scheduler_tick(self):
            return 0

        def singleton(self, name, *, ttl_seconds=120):
            @asynccontextmanager
            async def _cm():
                yield True

            return _cm()

    assert isinstance(Dummy(), ExecutionBackend)


# --- AC-2: registry resolution + lazy import ---------------------------------------------

def test_default_backend_is_local(monkeypatch):
    registry.reset_backend()
    monkeypatch.setattr(settings, "execution_backend", "local")
    try:
        assert isinstance(registry.get_backend(), LocalBackend)
    finally:
        registry.reset_backend()


def test_unknown_backend_raises_clearly(monkeypatch):
    registry.reset_backend()
    monkeypatch.setattr(settings, "execution_backend", "does-not-exist")
    try:
        with pytest.raises(RuntimeError) as ei:
            registry.get_backend()
        assert "does-not-exist" in str(ei.value)
    finally:
        registry.reset_backend()


def test_set_backend_override(monkeypatch):
    sentinel = LocalBackend()
    registry.set_backend(sentinel)
    try:
        assert registry.get_backend() is sentinel
    finally:
        registry.reset_backend()


# --- AC-4: singleton gate (Redis-less fallback to the leader flag) ------------------------

async def test_singleton_falls_back_to_leader_flag(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    b = LocalBackend()

    monkeypatch.setattr(settings, "scheduler_leader", True)
    async with b.singleton("reaper") as is_leader:
        assert is_leader is True

    monkeypatch.setattr(settings, "scheduler_leader", False)
    async with b.singleton("reaper") as is_leader:
        assert is_leader is False


# --- AC-5: the shared resume primitive does not re-execute completed work -----------------

_WF = {
    "id": "wf_cont", "version": 1,
    "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
    "entry_node": "agent",
    "nodes": [
        {"id": "agent", "type": "agent", "config": {"flavor": "agent", "model": "fake:echo"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [{"source": "agent", "target": "end"}],
}


def _ai_count(output) -> int:
    msgs = (output or {}).get("messages") or []
    return sum(1 for m in msgs if isinstance(m, dict) and (m.get("type") or m.get("role")) in ("ai", "assistant"))


async def test_continue_from_checkpoint_does_not_reexecute():
    async with SessionLocal() as s:
        wf = Workflow(tenant_id="t_cont", project_id="p_cont", name="Cont", executable=_WF, status="active")
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        wf_id = wf.id

    rs = RunService(checkpointer=InMemorySaver())
    async with SessionLocal() as s:
        run = await rs.create_run(
            s, tenant_id="t_cont", project_id="p_cont", workflow_id=wf_id,
            input={"messages": [{"role": "user", "content": "hi"}]}, source="test",
        )
        run_id = run.id

    res1 = await rs.run_to_completion(run_id=run_id, tenant_id="t_cont", project_id="p_cont")
    assert res1["status"] == "done"
    async with SessionLocal() as s:
        r = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        before = _ai_count(r.output)
    assert before >= 1

    # Re-driving from the checkpoint must NOT re-run the completed agent node.
    res2 = await rs._continue_from_checkpoint(run_id=run_id, tenant_id="t_cont", project_id="p_cont")
    assert res2["status"] == "done"
    assert res2["interrupted"] is False
    async with SessionLocal() as s:
        r = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        after = _ai_count(r.output)
    assert after == before
