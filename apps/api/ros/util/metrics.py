"""Tiny in-process metrics counter.

Many resilience paths swallow exceptions (a tool fails to materialize, an embedder is
unavailable, a channel reply doesn't send) so one failure can't break a run. That's
correct, but it must not be SILENT. `incr` bumps a named counter and logs once per
name; the counters are exposed at `/v1/metrics` (admin) so operators can see drift.
For multi-worker prod, scrape per-worker or push to a real metrics backend.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter

log = logging.getLogger("ros.metrics")

_counters: Counter[str] = Counter()
_lock = threading.Lock()
_logged: set[str] = set()


def incr(name: str, amount: int = 1, *, detail: str | None = None) -> None:
    with _lock:
        _counters[name] += amount
        first = name not in _logged
        if first:
            _logged.add(name)
    if first:
        log.warning("metric %s first occurrence%s", name, f": {detail}" if detail else "")


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    with _lock:
        _counters.clear()
        _logged.clear()


class RequestMetricsMiddleware:
    """Pure-ASGI RED-ish request counters (A/C5): `http.requests` (rate) and
    `http.responses.{2,3,4,5}xx` (errors, by status class), surfaced at `/v1/metrics`.

    Deliberately pure-ASGI (not Starlette's BaseHTTPMiddleware, which buffers the response body
    and would break SSE run streams) - it only peeks at the response START event for the status
    code and passes bytes through untouched. Duration histograms need a real metrics backend
    (OTel/Prometheus) and are intentionally left out of the in-process counter.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incr("http.requests")
        status = {"code": 500}  # default: if the app raises before sending a response, it's a 5xx

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            incr(f"http.responses.{status['code'] // 100}xx")
