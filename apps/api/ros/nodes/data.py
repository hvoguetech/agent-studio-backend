"""Data / integration nodes: transform, human_input, webhook_out, emit_event, emit_artifact.

Convention: data nodes read from an optional `input_key` (else the whole state)
and write to `output_key` (which MUST be a declared state field, else LangGraph
rejects the update). `human_input` writes the decision into `messages`.

(`tool_call` and the RAG node land next: tool_call needs per-user context
plumbing for auth'd tools; retrieval needs the Chroma store.)
"""

from __future__ import annotations

import logging
import re
from typing import Any

import jmespath

from ros.artifacts.state import emit as _emit_artifact
from ros.artifacts.state import run_scope
from ros.auth_providers.templates import render_value
from ros.engine.context import CompileContext
from ros.engine.registry import NodeSpec, Port, register

log = logging.getLogger("ros.data")


def _jq_transform(expr: str, input_key: str | None, output_key: str):
    """Build a jq-powered transform node. jq is optional; if the `jq` package isn't installed
    we raise a clear ValueError WHEN THE NODE RUNS rather than silently falling back to
    JMESPath (which speaks a different language and would quietly produce wrong data) - audit
    F7. Compile succeeds so the rest of the workflow still previews."""
    try:
        import jq as _jq
    except ImportError:
        def _unavailable(state: dict) -> dict:
            raise ValueError(
                "transform engine 'jq' requires the `jq` package, which is not installed. "
                "Install it (pip install jq) or switch this transform's engine to 'jmespath'."
            )
        return _unavailable

    try:
        program = _jq.compile(expr)  # a malformed program surfaces here, at compile time
    except Exception as e:  # noqa: BLE001 - re-raise as a clear config error
        raise ValueError(f"Invalid jq expression {expr!r}: {e}") from e

    def _node(state: dict) -> dict:
        src = state.get(input_key) if input_key else dict(state)
        try:
            result: Any = program.input(src).first()
        except Exception as e:  # noqa: BLE001 - a runtime jq failure -> None, but log it
            log.warning("transform jq %r failed: %s: %s", expr, type(e).__name__, e)
            result = None
        return {output_key: result}

    return _node


def transform_factory(cfg: dict, ctx: CompileContext):
    expr = cfg["expression"]
    engine = cfg.get("engine", "jmespath")
    input_key = cfg.get("input_key")
    output_key = cfg.get("output_key", "data")

    if engine == "jq":
        return _jq_transform(expr, input_key, output_key)

    def _node(state: dict) -> dict:
        src = state.get(input_key) if input_key else dict(state)
        try:
            result: Any = jmespath.search(expr, src)
        except jmespath.exceptions.JMESPathError as e:
            # Previously swallowed to None silently, which hid typo'd expressions; log it so a
            # broken transform is traceable in the run log (audit F7).
            log.warning("transform jmespath %r failed: %s: %s", expr, type(e).__name__, e)
            result = None
        return {output_key: result}

    return _node


def human_input_factory(cfg: dict, ctx: CompileContext):
    from langchain_core.messages import HumanMessage
    from langgraph.types import interrupt

    from ros.services.runs import HITL_APPROVAL_TIMEOUT_SECONDS

    prompt = cfg["prompt"]
    decisions = cfg.get("allowed_decisions", ["approve", "reject"])
    schema = cfg.get("schema")
    # When set, also write the decision string to this state key so a downstream router
    # can branch on it (approve → continue, reject → end). The key must be declared in
    # workflow state (the canvas auto-declares node-written keys).
    output_key = cfg.get("output_key")
    # Deadline surfaced on the interrupt so operators/UI see how long the approval waits before
    # the reaper expires it (audit C). Per-node override, else the global HITL timeout (0 = none).
    timeout_seconds = cfg.get("timeout_seconds") or HITL_APPROVAL_TIMEOUT_SECONDS or None
    timeout_default = cfg.get("timeout_default")
    if timeout_default not in decisions:
        timeout_default = None

    def _node(state: dict) -> dict:
        # Pauses the run; resumed via Command(resume=value). Node re-runs from the
        # top on resume, so the side effect (writing the decision) is placed after.
        decision = interrupt({
            "prompt": prompt, "allowed_decisions": decisions, "schema": schema,
            "timeout_seconds": timeout_seconds, "timeout_default": timeout_default,
        })
        out: dict[str, Any] = {"messages": [HumanMessage(content=f"[human decision] {decision}")]}
        if output_key:
            # Coerce a free-text resume value to one of allowed_decisions for the ROUTING key so a
            # Router keyed on approve/reject matches even on a direct API resume (audit C). The
            # transcript message above keeps the human's raw wording; only the routed value is
            # normalized. Structured (dict) input is left as-is.
            routed: Any = decision
            if isinstance(decision, str) and decisions:
                from ros.services.handoff import coerce_to_allowed_decision

                routed = coerce_to_allowed_decision(decision, list(decisions))
            out[output_key] = str(routed)
        return out

    return _node


def handoff_factory(cfg: dict, ctx: CompileContext):
    """Live-agent handoff: pause the run (interrupt) and hand the conversation to a
    human. The channel creates a HandoffRequest; when a human replies via the agent
    inbox, the run resumes with their text, which becomes the assistant's reply."""
    from langchain_core.messages import AIMessage
    from langgraph.types import interrupt

    reason = cfg.get("reason", "Escalated to a human agent.")

    def _node(state: dict) -> dict:
        reply = interrupt({"handoff": True, "reason": reason, "ack_message": cfg.get("ack_message")})
        return {"messages": [AIMessage(content=str(reply))]}

    return _node


def webhook_out_factory(cfg: dict, ctx: CompileContext):
    method = cfg["method"]
    url_t = cfg["url"]
    provider_id = cfg.get("auth_provider_id")
    output_key = cfg.get("output_key", "webhook_result")
    body_t = cfg.get("body")
    headers_t = cfg.get("headers", {})

    async def _node(state: dict) -> dict:
        from ros.util.http import shared_async_client
        from ros.util.ssrf import validate_url

        vars = {"state": dict(state)}
        url = render_value(url_t, vars)
        body = render_value(body_t, vars) if body_t else None
        headers = render_value(dict(headers_t), vars)
        params: dict[str, str] = {}
        cookies: dict[str, str] = {}
        if provider_id and ctx.auth_resolver:
            auth = await ctx.auth_resolver.resolve(
                tenant_id=ctx.tenant_id, project_id=ctx.project_id, provider_id=provider_id, context={}
            )
            headers.update(auth.headers)
            params.update(auth.params)
            cookies.update(auth.cookies)
        await validate_url(url, getattr(ctx, "egress_policy", None))
        c = shared_async_client()
        r = await c.request(method, url, headers=headers, params=params or None, json=body, cookies=cookies or None, timeout=30)
        try:
            out: Any = r.json()
        except Exception:  # noqa: BLE001
            out = r.text
        return {output_key: out}

    return _node


def tool_call_factory(cfg: dict, ctx: CompileContext):
    tool_id = cfg["tool_id"]
    input_mapping = cfg.get("input_mapping", {}) or {}
    output_key = cfg.get("output_key", "tool_result")

    async def _node(state: dict, config=None) -> dict:
        # Invoke the SAME materialized tool an agent would use, passing the run config so
        # the call is traced (the tracer is a callback on config) and so REST/GraphQL/
        # code/sql/mcp all go through one path with one error contract.
        tool = ctx.tool_registry.get(tool_id)
        if tool is None:
            return {output_key: {"error": f"tool {tool_id} not available"}}
        args: dict[str, Any] = {}
        for k, expr in input_mapping.items():
            try:
                args[k] = jmespath.search(expr, dict(state)) if isinstance(expr, str) else expr
            except jmespath.exceptions.JMESPathError:
                args[k] = expr
        try:
            out = await tool.ainvoke(args, config)
        except Exception as e:  # noqa: BLE001 - surface tool failure as a structured result
            out = {"error": str(e)}
        return {output_key: out}

    return _node


def emit_event_factory(cfg: dict, ctx: CompileContext):
    channel = cfg["channel"]
    payload_t = cfg.get("payload", {})

    def _node(state: dict) -> dict:
        try:
            from langgraph.config import get_stream_writer

            get_stream_writer()({"channel": channel, "payload": render_value(payload_t, {"state": dict(state)})})
        except Exception:  # noqa: BLE001 - no active stream writer (e.g. ainvoke)
            pass
        return {}

    return _node


def _last_message_text(state: dict) -> str:
    """Text of the last message on the `messages` channel — the usual upstream generator output
    (e.g. the HTML an agent/llm node just produced). Handles str content and text-block lists,
    and both LangChain message objects and plain dicts."""
    for msg in reversed(state.get("messages") or []):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(p for p in parts if p)
            if text:
                return text
    return ""


# A single fenced code block: ```lang\n…\n``` spanning the whole string. LLMs routinely wrap
# generated HTML/code in a fence, which we don't want inside the stored file.
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)


def _unwrap_fence(text: str) -> str:
    """Return the inner code if `text` is a single fenced block, else `text` unchanged."""
    m = _FENCE_RE.match(text or "")
    return m.group(1) if m else text


_CONTENT_TYPES = {
    "html": "text/html", "htm": "text/html", "css": "text/css", "js": "text/javascript",
    "mjs": "text/javascript", "json": "application/json", "svg": "image/svg+xml",
    "md": "text/markdown", "txt": "text/plain", "csv": "text/csv", "xml": "application/xml",
}


def _content_type_for(filename: str, explicit: str | None) -> str:
    """Explicit content type wins; otherwise infer from the filename extension (default text/plain)."""
    if explicit:
        return explicit
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _CONTENT_TYPES.get(ext, "text/plain")


def _stringify(val: Any) -> str:
    """Coerce a non-string source value to text: pretty JSON for dict/list, else str()."""
    if isinstance(val, (dict, list)):
        import json
        try:
            return json.dumps(val, indent=2, default=str)
        except (TypeError, ValueError):
            return str(val)
    return str(val)


def _emit_artifact_frame(entry: dict) -> None:
    """Emit an `artifact` stream frame (the produced file's ref + metadata) so the console can offer
    a download for it — artifact-plane files have no `Artifact` DB row, so this is how the UI learns
    about them. No-op when there's no active stream (e.g. ainvoke / non-streaming run)."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()({"channel": "artifact", "payload": entry})
    except Exception:  # noqa: BLE001 - no active stream writer / streaming unavailable
        pass


def _emit_html_preview(node_id: str | None, filename: str, html: str) -> None:
    """Emit a `component` stream frame carrying the raw HTML so the console renders it live in a
    SANDBOXED iframe — the same surface user-authored components use (sandbox=allow-scripts, no
    same-origin), which makes a generated mock UI interactive (clickable, JS runs) without letting
    it reach the host. No-op when there's no active stream (e.g. ainvoke / non-streaming run)."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()({
            "channel": "component",
            "payload": {
                "component_id": node_id or "mock_ui",
                "instance_id": f"{node_id or 'mock'}:preview",
                "name": filename,
                "html": html,          # rendered verbatim as the iframe document (raw=True)
                "raw": True,
                "props": {},
            },
        })
    except Exception:  # noqa: BLE001 - no active stream writer / streaming unavailable
        pass


def emit_artifact_factory(cfg: dict, ctx: CompileContext, node_id: str | None = None):
    """Write text (HTML, code, JSON, …) from state to a durable file artifact on the `artifacts`
    channel — the piece that lets a workflow END with a downloadable file (e.g. a generated mock
    UI's mockup.html). Bytes go to the artifact store (S3 in prod, local in dev); a tiny ref rides
    state so downstream nodes can consume it and the console can offer a presigned download.

    For HTML with `preview` on, it ALSO emits a live component frame so the console renders the mock
    interactively inline (sandboxed) — the artifact is the durable deliverable, the frame the preview."""
    source_key = (cfg.get("source_key") or "").strip()
    filename = (cfg.get("filename") or "artifact.txt").strip()
    explicit_ct = (cfg.get("content_type") or "").strip() or None
    unwrap = cfg.get("unwrap_code_fence", True)
    preview = cfg.get("preview", True)

    async def _node(state: dict, config=None) -> dict:
        val = state.get(source_key) if source_key else None
        text: str | None = None
        if isinstance(val, (bytes, bytearray)):
            data = bytes(val)  # raw bytes source: store verbatim, no text handling
        else:
            if source_key:
                text = val if isinstance(val, str) else (_last_message_text(state) if val is None else _stringify(val))
            else:
                text = _last_message_text(state)
            if unwrap:
                text = _unwrap_fence(text)
            data = (text or "").encode("utf-8")
        content_type = _content_type_for(filename, explicit_ct)
        entry = await _emit_artifact(
            tenant_id=ctx.tenant_id, project_id=ctx.project_id, run_id=run_scope(config),
            data=data, filename=filename, content_type=content_type, produced_by=node_id,
        )
        _emit_artifact_frame(entry)  # download chip in the console (any file type)
        if preview and content_type == "text/html":
            _emit_html_preview(node_id, filename, text if text is not None else data.decode("utf-8", "replace"))
        return {"artifacts": [entry]}

    return _node


_io_any = ([Port(id="in", io_type="any", direction="in")], [Port(id="out", io_type="any", direction="out")])

register(NodeSpec(
    type="transform", schema_id="ros/nodes/transform",
    input_ports=[Port(id="in", io_type="json", direction="in")],
    output_ports=[Port(id="out", io_type="json", direction="out")],
    factory=transform_factory, category="model_tools", label="Transform", description="JMESPath data map",
    summarize=lambda c: [f"{c.get('engine', 'jmespath')} · → {c.get('output_key', 'data')}"],
))
register(NodeSpec(
    type="human_input", schema_id="ros/nodes/human_input",
    input_ports=_io_any[0], output_ports=_io_any[1],
    factory=human_input_factory, category="human", label="Human Input", description="HITL pause via interrupt",
    summarize=lambda c: [c.get("prompt", "")[:40], " · ".join(c.get("allowed_decisions", ["approve", "reject"]))],
))
register(NodeSpec(
    type="tool_call", schema_id="ros/nodes/tool_call",
    input_ports=[Port(id="in", io_type="json", direction="in")],
    output_ports=[Port(id="out", io_type="json", direction="out")],
    factory=tool_call_factory, category="model_tools", label="Tool Call", description="Run a specific tool",
    summarize=lambda c: [str(c.get("tool_id", "-")), f"→ {c.get('output_key', 'tool_result')}"],
))
register(NodeSpec(
    type="webhook_out", schema_id="ros/nodes/webhook_out",
    input_ports=[Port(id="in", io_type="json", direction="in")],
    output_ports=[Port(id="out", io_type="json", direction="out")],
    factory=webhook_out_factory, category="integrations", label="Webhook", description="Call external URL",
    summarize=lambda c: [f"{c.get('method', 'POST')} {str(c.get('url', ''))[:32]}"],
))
register(NodeSpec(
    type="handoff", schema_id="ros/nodes/handoff",
    input_ports=_io_any[0], output_ports=_io_any[1],
    factory=handoff_factory, category="human", label="Human Handoff",
    description="Escalate the conversation to a human agent (pauses until they reply).",
    summarize=lambda c: [c.get("reason", "human handoff")[:40]],
))
register(NodeSpec(
    type="emit_event", schema_id="ros/nodes/emit_event",
    input_ports=_io_any[0], output_ports=_io_any[1],
    factory=emit_event_factory, category="integrations", label="Emit Event", description="Push custom SSE frame",
    summarize=lambda c: [f"channel · {c.get('channel', '')}"],
))
register(NodeSpec(
    type="emit_artifact", schema_id="ros/nodes/emit_artifact",
    input_ports=_io_any[0], output_ports=_io_any[1],
    factory=emit_artifact_factory, category="integrations", label="Emit Artifact",
    description="Write text/HTML from state to a downloadable file artifact",
    summarize=lambda c: [c.get("filename", "artifact.txt"), c.get("content_type") or "auto type"],
))
