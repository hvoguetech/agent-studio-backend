"""Run stream relay (A/C3) — replica-/VM-portable SSE. The relay mirrors every broker frame to a
shared bus so the SSE endpoint can serve a run driven in another process. Tested by injecting an
in-memory bus double (the repo's redis-test convention: no fakeredis, no live Redis in pytest)."""

from __future__ import annotations

import asyncio

import pytest

from ros.services.run_relay import (
    InMemoryRelayBus,
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

    broker = _RunBroker("r_mirror")
    await broker.publish(_frame("token", text="x"))
    buffered = await bus.buffered("r_mirror")
    assert buffered and buffered[0]["seq"] == 1 and buffered[0]["frame"]["event"] == "token"


async def test_broker_without_run_id_does_not_mirror(bus):
    from ros.services.runs import _RunBroker

    broker = _RunBroker("")  # anonymous broker (belt-and-suspenders): nothing to mirror against
    await broker.publish(_frame("token"))
    assert await bus.buffered("") == []
