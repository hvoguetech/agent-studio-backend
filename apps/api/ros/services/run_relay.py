"""Run stream relay — replica-/VM-portable live streaming (A/C3).

The in-process `_RunBroker` fans out only to subscribers in THIS process. When a run's driver is a
DIFFERENT process — another master replica, or a Freestyle VM running the standalone runtime — the
SSE endpoint here has no local broker (today it degrades to a "not streaming on this server" error).
This relay mirrors every frame to a shared bus (Redis in prod) keyed by run_id, so the SSE endpoint
can subscribe and relay frames produced anywhere.

The bus sits behind a tiny seam (`RelayBus`) so the suite injects an in-memory double instead of a
live Redis — matching the repo's redis-test convention (no fakeredis, no live Redis in pytest).
Disabled (no-op) when `settings.redis_url` is unset, so single-process behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ros.config import settings

log = logging.getLogger("ros.run_relay")

_BUF_MAX = 512          # frames kept for Last-Event-ID replay per run
_BUF_TTL_S = 3600       # relay buffer lifetime
_TERMINAL = {"done", "error", "canceled"}


def _channel(run_id: str) -> str:
    return f"ros:run:{run_id}"


@runtime_checkable
class RelayBus(Protocol):
    async def publish(self, run_id: str, item: dict) -> None: ...
    async def buffered(self, run_id: str) -> list[dict]: ...
    def subscribe(self, run_id: str) -> AsyncIterator[dict]: ...


class RedisRelayBus:
    """Redis-backed bus: pub/sub for live fan-out + a capped list for Last-Event-ID replay.

    ⚠️ LIVE-VERIFY: exercised in the suite only via the in-memory double; confirm against a real
    Redis on first deploy (pub/sub delivery + the LPUSH/LTRIM replay buffer)."""

    def __init__(self, url: str) -> None:
        self._url = url

    def _client(self):
        from redis.asyncio import from_url
        return from_url(self._url)

    async def publish(self, run_id: str, item: dict) -> None:
        payload = json.dumps(item, default=str)
        redis = self._client()
        try:
            buf = f"{_channel(run_id)}:buf"
            await redis.rpush(buf, payload)
            await redis.ltrim(buf, -_BUF_MAX, -1)
            await redis.expire(buf, _BUF_TTL_S)
            await redis.publish(_channel(run_id), payload)
        finally:
            await redis.aclose()

    async def buffered(self, run_id: str) -> list[dict]:
        redis = self._client()
        try:
            raw = await redis.lrange(f"{_channel(run_id)}:buf", 0, -1)
        finally:
            await redis.aclose()
        return [json.loads(r) for r in raw]

    async def subscribe(self, run_id: str) -> AsyncIterator[dict]:
        redis = self._client()
        pubsub = redis.pubsub()
        await pubsub.subscribe(_channel(run_id))
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                data = msg.get("data")
                yield json.loads(data.decode() if isinstance(data, bytes) else data)
        finally:
            await pubsub.unsubscribe(_channel(run_id))
            await pubsub.aclose()
            await redis.aclose()


class InMemoryRelayBus:
    """Process-local bus double for tests — same contract as RedisRelayBus, no external service."""

    def __init__(self) -> None:
        self._buf: dict[str, list[dict]] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}

    async def publish(self, run_id: str, item: dict) -> None:
        self._buf.setdefault(run_id, []).append(item)
        for q in self._subs.get(run_id, set()):
            q.put_nowait(item)

    async def buffered(self, run_id: str) -> list[dict]:
        return list(self._buf.get(run_id, []))

    async def subscribe(self, run_id: str) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(run_id, set()).add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.get(run_id, set()).discard(q)


_bus: RelayBus | None = None
_resolved = False


def get_relay_bus() -> RelayBus | None:
    global _bus, _resolved
    if not _resolved:
        _resolved = True
        if settings.redis_url:
            _bus = RedisRelayBus(settings.redis_url)
    return _bus


def set_relay_bus(bus: RelayBus | None) -> None:
    """Override the bus (tests / embedding)."""
    global _bus, _resolved
    _bus, _resolved = bus, True


async def publish_frame(run_id: str, seq: int, frame: dict) -> None:
    """Best-effort mirror of one broker frame to the shared bus (no-op without a bus)."""
    bus = get_relay_bus()
    if bus is None:
        return
    try:
        await bus.publish(run_id, {"seq": seq, "frame": frame})
    except Exception:  # noqa: BLE001 - a relay hiccup must never break the in-process stream
        log.debug("relay publish failed for %s", run_id, exc_info=True)


async def relay_frames(run_id: str, last_event_id: int = 0) -> AsyncIterator[tuple[int, dict]]:
    """Relay a run's frames from the shared bus (buffered replay past `last_event_id`, then live),
    stopping after a terminal frame. Empty when no bus is configured."""
    bus = get_relay_bus()
    if bus is None:
        return
    seen = last_event_id
    for item in await bus.buffered(run_id):
        seq = int(item.get("seq") or 0)
        frame = item.get("frame") or {}
        if seq > seen:
            yield seq, frame
            seen = seq
            if frame.get("event") in _TERMINAL:
                return
    async for item in bus.subscribe(run_id):
        seq = int(item.get("seq") or 0)
        frame = item.get("frame") or {}
        if seq > seen:
            yield seq, frame
            seen = seq
            if frame.get("event") in _TERMINAL:
                return
