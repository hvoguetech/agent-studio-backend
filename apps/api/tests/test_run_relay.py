"""Run stream relay (A/C3) — replica-/VM-portable SSE. The relay mirrors every broker frame to a
shared bus so the SSE endpoint can serve a run driven in another process. Tested by injecting an
in-memory bus double (the repo's redis-test convention: no fakeredis, no live Redis in pytest)."""

from __future__ import annotations

import asyncio

import pytest

from ros.services.run_relay import (
    InMemoryRelayBus,
    _channel,
    publish_frame,
    relay_frames,
    set_relay_bus,
)


@pytest.fixture
def bus():
    b = InMemoryRelayBus()
    set_relay_bus(b)
    yield b
    set_relay_bus(None)  # unconfigured for the rest of the suite (matches redis_url=None default)


def _frame(event: str, **data) -> dict:
    return {"event": event, "data": data}


async def test_publish_relay_round_trip_via_buffer(bus):
    await publish_frame("r1", 1, _frame("token", text="a"))
    await publish_frame("r1", 2, _frame("done", answer="hi"))
    got = [(seq, f["event"]) async for seq, f in relay_frames("r1", 0)]
    assert got == [(1, "token"), (2, "done")]  # replays the buffer, stops after the terminal frame


async def test_relay_respects_last_event_id(bus):
    await publish_frame("r1", 1, _frame("token"))
    await publish_frame("r1", 2, _frame("done"))
    got = [seq async for seq, _ in relay_frames("r1", 1)]
    assert got == [2]  # frame 1 was already delivered before the reconnect


async def test_relay_follows_live_frames(bus):
    await publish_frame("r1", 1, _frame("token", text="a"))  # buffered before the subscriber joins

    async def collect():
        return [(seq, f["event"]) async for seq, f in relay_frames("r1", 0)]

    task = asyncio.create_task(asyncio.wait_for(collect(), timeout=5))
    await asyncio.sleep(0.05)  # let the relay drain the buffer and start following live
    await publish_frame("r1", 2, _frame("done", answer="hi"))  # live terminal frame
    got = await task
    assert got == [(1, "token"), (2, "done")]


async def test_relay_isolates_runs(bus):
    await publish_frame("r1", 1, _frame("done"))
    await publish_frame("r2", 1, _frame("token"))
    got_r1 = [f["event"] async for _, f in relay_frames("r1", 0)]
    assert got_r1 == ["done"]  # r2's frame is on a different channel


async def test_no_bus_is_noop():
    set_relay_bus(None)
    await publish_frame("r1", 1, _frame("token"))  # must not raise without a bus
    got = [x async for x in relay_frames("r1", 0)]
    assert got == []


async def test_broker_publish_mirrors_to_bus(bus):
    from ros.services.runs import _RunBroker

    broker = _RunBroker("r_mirror", "t_m")
    await broker.publish(_frame("token", text="x"))
    buffered = await bus.buffered(_channel("r_mirror", "t_m"))
    assert buffered and buffered[0]["seq"] == 1 and buffered[0]["frame"]["event"] == "token"


async def test_broker_without_run_id_does_not_mirror(bus):
    from ros.services.runs import _RunBroker

    broker = _RunBroker("")  # anonymous broker (belt-and-suspenders): nothing to mirror against
    await broker.publish(_frame("token"))
    assert await bus.buffered(_channel("")) == []


async def test_relay_is_tenant_namespaced(bus):
    # The same run_id under two tenants lands on SEPARATE channels (defense-in-depth on shared Redis).
    await publish_frame("shared_rid", 1, _frame("done"), tenant_id="t1")
    # t1's relay sees its own terminal frame and returns; t2's channel is empty. (We assert t2 via
    # buffered(), NOT a relay_frames drain - relay_frames blocks on a quiet channel by design, since
    # in production only the watchdog-wrapped pump drives it and cancels it.)
    got_t1 = [f["event"] async for _, f in relay_frames("shared_rid", 0, tenant_id="t1")]
    assert got_t1 == ["done"]
    assert await bus.buffered(_channel("shared_rid", "t1"))  # keyed under the tenant prefix
    assert await bus.buffered(_channel("shared_rid", "t2")) == []  # isolated: nothing under t2
