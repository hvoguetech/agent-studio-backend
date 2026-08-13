"""Trusted-VM driver (increment 2): drive a run on the VM via the SAME RunService._drive, finalize
the shared DB row, and mirror every frame to the relay bus so master relays the VM's stream (A/C3).
Uses an in-memory bus double + an in-process saver (no live Redis / no VM), per the repo convention.
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ros.db.base import SessionLocal
from ros.models import Run, Thread, Workflow
from ros.runtime.driver import drive_run
from ros.services.run_relay import InMemoryRelayBus, _channel, set_relay_bus

_ANSWER = "Driven on the VM."
_WF = {
    "id": "wf_vm", "version": 1,
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
def bus():
    b = InMemoryRelayBus()
    set_relay_bus(b)
    yield b
    set_relay_bus(None)


async def test_drive_run_finalizes_db_and_mirrors_stream_to_bus(bus):
    t, p, rid = await _seed_queued_run()

    await drive_run(run_id=rid, tenant_id=t, project_id=p, checkpointer=InMemorySaver())

    # _drive finalized the shared DB row itself (no separate callback needed).
    async with SessionLocal() as s:
        run = await s.get(Run, rid)
        assert run.status == "done", run.status

    # Every frame reached the relay bus, monotonic from 1, opening with `run` and terminating in a
    # `done` carrying the final answer - i.e. master would relay an identical stream.
    frames = await bus.buffered(_channel(rid, t))
    seqs = [f["seq"] for f in frames]
    events = [f["frame"]["event"] for f in frames]
    assert seqs == sorted(seqs) and seqs[0] == 1
    assert events[0] == "run" and "done" in events
    done = [f["frame"] for f in frames if f["frame"]["event"] == "done"][-1]
    assert _ANSWER in (done["data"].get("answer") or "")


async def test_drive_run_is_noop_streamwise_without_a_bus():
    # No bus configured (default) -> the driver still finalizes the DB; frame mirroring is a no-op.
    set_relay_bus(None)
    t, p, rid = await _seed_queued_run()
    await drive_run(run_id=rid, tenant_id=t, project_id=p, checkpointer=InMemorySaver())
    async with SessionLocal() as s:
        run = await s.get(Run, rid)
        assert run.status == "done", run.status
