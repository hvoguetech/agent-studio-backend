"""OpenTelemetry export emits GenAI-semconv spans from run SpanRecords."""

from __future__ import annotations

from ros.tracing import otel
from ros.tracing.tracer import SpanRecord


def test_export_emits_spans_with_genai_attributes():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    assert otel.configure(exporter=exporter) is True
    assert otel.enabled() is True

    records = [
        SpanRecord(id="s1", parent_id=None, name="agent_1", kind="llm", start=1000.0, end=1000.5,
                   start_wall=1_700_000_000.0, end_wall=1_700_000_000.5,
                   model="openai:gpt-4o-mini", input_tokens=120, output_tokens=30, cost_usd=0.0001),
        SpanRecord(id="s2", parent_id="s1", name="get_order", kind="tool", start=1000.5, end=1000.6,
                   start_wall=1_700_000_000.5, end_wall=1_700_000_000.6, error="boom"),
    ]
    otel.export(records, trace_name="run")

    spans = exporter.get_finished_spans()
    # One root span ("run") now groups the child spans into a single trace with a real hierarchy.
    assert len(spans) == 3
    by_name = {s.name: s for s in spans}
    root, llm, tool = by_name["run"], by_name["agent_1"], by_name["get_order"]
    assert llm.attributes["gen_ai.request.model"] == "openai:gpt-4o-mini"
    assert llm.attributes["gen_ai.system"] == "openai"
    assert llm.attributes["gen_ai.usage.input_tokens"] == 120
    assert tool.attributes["error"] is True
    # Wall-clock start time (post-2020 in ns), NOT the ~1970 that exporting monotonic seconds gave.
    assert llm.start_time > 1_600_000_000_000_000_000
    # Single trace, correct parent/child nesting: run -> agent_1 -> get_order.
    assert root.context.trace_id == llm.context.trace_id == tool.context.trace_id
    assert root.parent is None
    assert llm.parent.span_id == root.context.span_id
    assert tool.parent.span_id == llm.context.span_id


def test_export_emits_llm_content_events():
    """Captured prompt/completion (#57) export as GenAI content events on the llm span."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    assert otel.configure(exporter=exporter) is True

    rec = SpanRecord(id="s1", parent_id=None, name="model", kind="llm", start=1000.0, end=1000.5,
                     start_wall=1_700_000_000.0, end_wall=1_700_000_000.5, model="openai:gpt-4o-mini",
                     input=[{"role": "human", "content": "hi there"}], output="hello!")
    otel.export([rec], trace_name="run")

    llm = {s.name: s for s in exporter.get_finished_spans()}["model"]
    events = {e.name: e for e in llm.events}
    assert "gen_ai.content.prompt" in events and "gen_ai.content.completion" in events
    assert events["gen_ai.content.completion"].attributes["gen_ai.completion"] == "hello!"
    assert "hi there" in events["gen_ai.content.prompt"].attributes["gen_ai.prompt"]


def test_export_noop_when_unconfigured():
    # reset to unconfigured state
    otel._tracer = None
    otel.export([SpanRecord(id="x", parent_id=None, name="n", kind="node", start=0.0, end=1.0)])  # must not raise
