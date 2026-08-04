"""A/C9 - orphan-run reclaim & auto-resume (LocalBackend crash recovery).

Covers the run lease columns (AC-1), the heartbeat lease (AC-2), orphan detection + checkpoint
re-drive incl. the no-Redis boot-scan (AC-3/5/7/8), the interrupted-exclusion (AC-4), and the
reclaim-attempt cap / dead-letter (AC-10). The re-drive primitive itself (no re-execution) is
covered by test_execution_backend.py (AC-5/6).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import Run, Workflow
from ros.services.runs import RunService, run_control, worker_id
from ros.util.metrics import snapshot

_WF = {
    "id": "wf_rec", "version": 1,
    "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
    "entry_node": "agent",
    "nodes": [
        {"id": "agent", "type": "agent", "config": {"flavor": "agent", "model": "fake:echo"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [{"source": "agent", "target": "end"}],
}


async def _new_run(**overrides) -> str:
    fields = dict(
        tenant_id="t", project_id="p", workflow_id="w", thread_id="th",
        status="running", input={},
    )
    fields.update(overrides)
    async with SessionLocal() as s:
        run = Run(**fields)
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run.id


async def _get(run_id: str) -> Run:
    async with SessionLocal() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


# --- AC-1: lease columns exist and back-fill safely --------------------------------------

async def test_run_has_reclaim_lease_columns():
    run_id = await _new_run(tenant_id="t_col", status="queued")
    run = await _get(run_id)
    assert run.reclaim_attempts == 0
    assert run.owner_id is None
    assert run.heartbeat_at is None


# --- AC-2: heartbeat stamps owner + advances while the driver lives -----------------------

async def test_heartbeat_stamps_owner_and_advances(monkeypatch):
    monkeypatch.setattr(settings, "run_heartbeat_interval_seconds", 1)
    run_id = await _new_run(tenant_id="t_hb")
    run_control.begin(run_id, "t_hb")
    try:
        await asyncio.sleep(0.3)  # first beat is immediate
        first = await _get(run_id)
        assert first.owner_id == worker_id()
        assert first.heartbeat_at is not None
        await asyncio.sleep(1.3)  # cross one interval
        second = await _get(run_id)
        assert second.heartbeat_at > first.heartbeat_at
    finally:
        await run_control.end(run_id)


# --- AC-3/5/7/8: a stale (or NULL) heartbeat running run is reclaimed to a terminal state --

async def _completed_run(tenant: str) -> tuple[RunService, str]:
    async with SessionLocal() as s:
        wf = Workflow(tenant_id=tenant, project_id="p", name="Rec", executable=_WF, status="active")
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        wf_id = wf.id
    rs = RunService(checkpointer=InMemorySaver())
    async with SessionLocal() as s:
        run = await rs.create_run(
            s, tenant_id=tenant, project_id="p", workflow_id=wf_id,
            input={"messages": [{"role": "user", "content": "hi"}]}, source="test",
        )
        run_id = run.id
    res = await rs.run_to_completion(run_id=run_id, tenant_id=tenant, project_id="p")
    assert res["status"] == "done"
    return rs, run_id


async def _mark_orphan(run_id: str, *, heartbeat_at) -> None:
    async with SessionLocal() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        run.status = "running"
        run.heartbeat_at = heartbeat_at
        run.reclaim_attempts = 0
        await s.commit()


async def test_stale_heartbeat_running_run_is_reclaimed():
    rs, run_id = await _completed_run("t_rec_stale")
    await _mark_orphan(run_id, heartbeat_at=datetime.utcnow() - timedelta(seconds=999))
    before = snapshot().get("runs.reclaimed", 0)
    n = await rs.reclaim_running_orphans()
    assert n >= 1
    run = await _get(run_id)
    assert run.status == "done"  # re-driven from checkpoint to a terminal state
    assert run.owner_id is not None  # leased during reclaim
    assert snapshot().get("runs.reclaimed", 0) >= before + 1  # AC-11 metric counter


async def test_null_heartbeat_running_run_is_reclaimed_boot_scan():
    # No heartbeat at all = a run left 'running' by a crashed/restarted process (the boot scan).
    rs, run_id = await _completed_run("t_rec_null")
    await _mark_orphan(run_id, heartbeat_at=None)
    n = await rs.reclaim_running_orphans()
    assert n >= 1
    assert (await _get(run_id)).status == "done"


# --- AC-3 (negative): a healthy (fresh-heartbeat) running run is NOT reclaimed -------------

async def test_fresh_heartbeat_not_reclaimed():
    run_id = await _new_run(tenant_id="t_fresh", heartbeat_at=datetime.utcnow(), reclaim_attempts=0)
    rs = RunService(checkpointer=InMemorySaver())
    await rs.reclaim_running_orphans()
    run = await _get(run_id)
    assert run.status == "running"
    assert (run.reclaim_attempts or 0) == 0


# --- AC-4: interrupted (HITL) runs are never reclaimed ------------------------------------

async def test_interrupted_not_reclaimed():
    run_id = await _new_run(
        tenant_id="t_int", status="interrupted",
        heartbeat_at=datetime.utcnow() - timedelta(seconds=999), reclaim_attempts=0,
    )
    rs = RunService(checkpointer=InMemorySaver())
    await rs.reclaim_running_orphans()
    run = await _get(run_id)
    assert run.status == "interrupted"
    assert (run.reclaim_attempts or 0) == 0


# --- AC-10: a run that exhausts its attempts is dead-lettered, not looped ------------------

async def test_reclaim_attempts_capped_dead_letters(monkeypatch):
    monkeypatch.setattr(settings, "run_max_reclaim_attempts", 3)
    run_id = await _new_run(
        tenant_id="t_cap", heartbeat_at=datetime.utcnow() - timedelta(seconds=999),
        reclaim_attempts=3,
    )
    rs = RunService(checkpointer=InMemorySaver())
    before = snapshot().get("runs.reclaim_dead_lettered", 0)
    await rs.reclaim_running_orphans()
    run = await _get(run_id)
    assert run.status == "error"
    assert "exhausted" in (run.error or "")
    assert run.reclaim_attempts == 4
    assert snapshot().get("runs.reclaim_dead_lettered", 0) >= before + 1  # AC-11 metric counter
