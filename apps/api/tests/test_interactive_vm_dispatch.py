"""Interactive-on-VM dispatch (final increment): under the Freestyle backend, stream() atomically
claims a fresh run, dispatches it to a VM (backend.submit), and RELAYS its stream off the bus -
instead of driving locally. A second caller loses the claim (no double-driver). Dispatch failure
falls back to a local drive. Off by default, so the normal interactive path is unchanged.
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import Run, Thread, Workflow
from ros.services.run_relay import InMemoryRelayBus, publish_frame, set_relay_bus
from ros.services.runs import RunService, run_streams

_ANSWER = "Local fallback answer."
_WF = {
    "id": "wf_disp", "version": 1,
    "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
    "entry_node": "agent",
    "nodes": [
        {"id": "agent", "type": "agent", "config": {"flavor": "agent", "model": f"fake:{_ANSWER}", "tools": []}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [{"source": "agent", "target": "end"}],
}


async def _seed_queued_run() -> tuple[str, str, str]:
    t, p = f"t_{uuid.uuid4().hex[:8]}", f"p_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as s:
        wf = Workflow(tenant_id=t, project_id=p, name="w", executable=_WF, status="active")
        s.add(wf)
        await s.flush()
        thread = Thread(tenant_id=t, project_id=p, workflow_id=wf.id, lg_thread_id=f"lg_{uuid.uuid4().hex}", meta={})
        s.add(thread)
        await s.flush()
        run = Run(tenant_id=t, project_id=p, workflow_id=wf.id, thread_id=thread.id, status="queued",
                  input={"messages": [{"role": "user", "content": "hi"}]})
        s.add(run)
        await s.commit()
        return t, p, run.id


@pytest.fixture
def freestyle(monkeypatch):
    """Enable VM dispatch + a relay bus double for the duration of a test."""
    monkeypatch.setattr(settings, "execution_backend", "freestyle")
    monkeypatch.setattr(settings, "freestyle_service_url", "http://svc")
    bus = InMemoryRelayBus()
    set_relay_bus(bus)
    yield bus
    set_relay_bus(None)


async def test_claim_for_dispatch_has_a_single_winner():
    t, p, rid = await _seed_queued_run()
    rs = RunService()
    assert await rs._claim_for_dispatch(rid, t) is True   # first caller claims queued -> running
    assert await rs._claim_for_dispatch(rid, t) is False  # run is no longer queued -> loses
    async with SessionLocal() as s:
        assert (await s.get(Run, rid)).status == "running"


async def test_stream_dispatches_to_vm_and_relays(freestyle, monkeypatch):
    t, p, rid = await _seed_queued_run()
    submitted: dict = {}

    class _FakeBackend:
        async def submit(self, *, run_id, tenant_id, project_id=None):
            submitted.update(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
            # Simulate the VM driving + publishing its stream to the shared bus.
            await publish_frame(run_id, 1, {"event": "run", "data": {"run_id": run_id}})
            await publish_frame(run_id, 2, {"event": "messages", "data": {"content": "hi"}})
            await publish_frame(run_id, 3, {"event": "done", "data": {"status": "done", "answer": "hi"}})
            return {"status": "dispatched"}

    monkeypatch.setattr("ros.execution.get_backend", lambda: _FakeBackend())

    rs = RunService()
    frames = [f async for f in rs.stream(run_id=rid, tenant_id=t, project_id=p)]
    events = [f["event"] for f in frames]

    assert submitted["run_id"] == rid and submitted["project_id"] == p  # dispatched to the VM
    assert events == ["run", "messages", "done"]  # relayed the VM's stream, stopping at the terminal
    assert run_streams.get(rid) is None  # master did NOT start a local driver (no double-driver)


async def test_lost_claim_relays_without_a_second_dispatch(freestyle, monkeypatch):
    t, p, rid = await _seed_queued_run()
    # Another caller already claimed + dispatched: run is 'running' and its frames are on the bus.
    rs = RunService()
    assert await rs._claim_for_dispatch(rid, t) is True
    await publish_frame(rid, 1, {"event": "run", "data": {"run_id": rid}})
    await publish_frame(rid, 2, {"event": "done", "data": {"status": "done", "answer": "hi"}})

    calls = {"n": 0}

    class _FakeBackend:
        async def submit(self, **kw):
            calls["n"] += 1
            return {"status": "dispatched"}

    monkeypatch.setattr("ros.execution.get_backend", lambda: _FakeBackend())

    frames = [f async for f in rs.stream(run_id=rid, tenant_id=t, project_id=p)]
    assert calls["n"] == 0  # lost the claim -> must NOT dispatch again
    assert [f["event"] for f in frames] == ["run", "done"]  # relayed only


async def test_stream_falls_back_to_local_when_dispatch_fails(freestyle, monkeypatch):
    t, p, rid = await _seed_queued_run()

    class _BrokenBackend:
        async def submit(self, **kw):
            raise RuntimeError("freestyle-svc down")

    monkeypatch.setattr("ros.execution.get_backend", lambda: _BrokenBackend())

    rs = RunService(checkpointer=InMemorySaver())
    frames = [f async for f in rs.stream(run_id=rid, tenant_id=t, project_id=p)]
    events = [f["event"] for f in frames]
    assert "done" in events  # drove to completion locally as a fallback
    done = [f for f in frames if f["event"] == "done"][-1]
    assert _ANSWER in (done["data"].get("answer") or "")
    async with SessionLocal() as s:
        assert (await s.get(Run, rid)).status == "done"
