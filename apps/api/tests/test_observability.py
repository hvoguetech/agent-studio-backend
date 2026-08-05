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


def test_llm_metrics_counted():
    """Per-call LLM counters land at /v1/metrics via the tracer callbacks (#59)."""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from ros.tracing.tracer import ROSTracer

    metrics.reset()
    tr = ROSTracer()
    # one successful call (a provider:model id so the by-provider counter fires)
    tr.on_llm_start({"name": "openai:gpt-4.1"}, ["p"], run_id="ok")
    msg = AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    tr.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id="ok")
    # one failed call
    tr.on_llm_start({"name": "openai:gpt-4.1"}, ["p"], run_id="bad")
    tr.on_llm_error(RuntimeError("boom"), run_id="bad")

    snap = metrics.snapshot()
    assert snap.get("llm.calls") == 2            # success + error both count as calls (for an error rate)
    assert snap.get("llm.errors") == 1
    assert snap.get("llm.tokens.input") == 10
    assert snap.get("llm.tokens.output") == 5
    assert snap.get("llm.calls.openai") == 2     # bounded by-provider breakdown
    assert snap.get("llm.errors.openai") == 1


def _drive_llm(tr, *, prompt_msgs, completion, run_id="x"):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    tr.on_chat_model_start({"name": "openai:gpt-4.1"}, [prompt_msgs], run_id=run_id)
    msg = AIMessage(content=completion, usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    tr.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id=run_id)
    return tr.spans[run_id]


def test_llm_io_captured_when_enabled(monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage

    from ros.tracing.tracer import ROSTracer

    monkeypatch.setattr(settings, "trace_llm_io", True)
    monkeypatch.setattr(settings, "trace_tool_io_redact", False)
    monkeypatch.setattr(settings, "environment", "development")  # not prod -> not force-redacted
    sp = _drive_llm(ROSTracer(), prompt_msgs=[SystemMessage(content="be terse"), HumanMessage(content="hi there")],
                    completion="hello!")
    assert sp.kind == "llm"
    assert {m["role"]: m["content"] for m in sp.input} == {"system": "be terse", "human": "hi there"}
    assert sp.output == "hello!"


def test_llm_io_off_by_default(monkeypatch):
    from langchain_core.messages import HumanMessage

    from ros.tracing.tracer import ROSTracer

    monkeypatch.setattr(settings, "trace_llm_io", False)
    sp = _drive_llm(ROSTracer(), prompt_msgs=[HumanMessage(content="secret question")], completion="answer")
    assert sp.input is None and sp.output is None  # no prompt/completion captured


def test_llm_io_redacted_masks_content(monkeypatch):
    from langchain_core.messages import HumanMessage

    from ros.tracing.tracer import ROSTracer

    monkeypatch.setattr(settings, "trace_llm_io", True)
    monkeypatch.setattr(settings, "trace_tool_io_redact", True)  # redaction on -> length placeholder
    sp = _drive_llm(ROSTracer(), prompt_msgs=[HumanMessage(content="my SSN is 123-45-6789")],
                    completion="noted 123-45-6789")
    assert "123-45-6789" not in json.dumps(sp.input) and "123-45-6789" not in str(sp.output)
    assert "•••" in str(sp.input) and "•••" in str(sp.output)
