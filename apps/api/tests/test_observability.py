"""A/C5 observability: structured JSON logging + RED-ish request counters."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from ros.config import settings
from ros.main import create_app
from ros.util import metrics
from ros.util.logging_setup import JsonFormatter, configure_logging


def _record(**extra) -> logging.LogRecord:
    return logging.LogRecord(
        name="ros.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None, **extra,
    )


def test_json_formatter_emits_valid_line_with_core_fields():
    line = JsonFormatter().format(_record())
    payload = json.loads(line)  # must be a single valid JSON object
    assert payload["level"] == "INFO"
    assert payload["logger"] == "ros.test"
    assert payload["msg"] == "hello world"  # %-args interpolated
    assert payload["ts"]


def test_json_formatter_includes_extra_and_survives_unserializable():
    rec = _record()
    rec.run_id = "abc-123"          # structured extra=
    rec.blob = object()             # not JSON-serializable -> must be stringified, not crash
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["run_id"] == "abc-123"
    assert isinstance(payload["blob"], str)


def test_json_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = logging.LogRecord(
            "ros.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in payload["exc"]


def test_configure_logging_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "log_json", False)
    root = logging.getLogger()
    before = list(root.handlers)
    configure_logging()
    assert root.handlers == before  # untouched: pytest/uvicorn keep their handlers


def test_configure_logging_installs_json_handler_when_enabled(monkeypatch):
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        monkeypatch.setattr(settings, "log_json", True)
        monkeypatch.setattr(settings, "log_level", "WARNING")
        configure_logging()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING
    finally:
        root.handlers, root.level = saved_handlers, saved_level


@pytest.mark.asyncio
async def test_request_metrics_middleware_counts_requests():
    metrics.reset()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/livez")  # public health endpoint, no auth
    assert resp.status_code == 200
    snap = metrics.snapshot()
    assert snap.get("http.requests", 0) >= 1
    assert snap.get("http.responses.2xx", 0) >= 1
