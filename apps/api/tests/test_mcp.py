"""MCP tool-kind wiring tests (monkeypatched server - no live MCP connection)."""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from ros.db.base import SessionLocal
from ros.models import McpClient
from ros.services.runtime import make_runtime_ctx
from ros.tools import mcp as mcp_mod
from ros.tools.mcp import McpUnavailable, load_mcp_tool


def _fake_remote_tool(name="search"):
    async def _run(q: str) -> str:
        return f"results for {q}"
    return StructuredTool.from_function(coroutine=_run, name=name, description="remote search")


async def _make_client(tenant="t_mcp", project="p_mcp") -> str:
    async with SessionLocal() as s:
        row = McpClient(tenant_id=tenant, project_id=project, name="demo", transport="streamable_http", url="https://mcp.example/sse")
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def test_adapters_import_available():
    # The optional extra is installed in this env; the loader resolves the client class.
    assert mcp_mod._require_adapters() is not None


async def test_load_mcp_tool_finds_remote_tool(monkeypatch):
    cid = await _make_client()

    async def fake_client_and_tools(row, tenant_id, project_id):
        return object(), [_fake_remote_tool("search")]

    monkeypatch.setattr(mcp_mod, "_client_and_tools", fake_client_and_tools)
    ctx = make_runtime_ctx("t_mcp", "p_mcp")
    tool = await load_mcp_tool({"mcp_client_id": cid, "remote_tool_name": "search"}, ctx)
    assert tool.name == "search"
    assert await tool.ainvoke({"q": "hello"}) == "results for hello"


async def test_load_mcp_tool_unknown_remote(monkeypatch):
    cid = await _make_client()
    monkeypatch.setattr(mcp_mod, "_client_and_tools", lambda *a, **k: _noop([]))
    ctx = make_runtime_ctx("t_mcp", "p_mcp")
    with pytest.raises(McpUnavailable):
        await load_mcp_tool({"mcp_client_id": cid, "remote_tool_name": "nope"}, ctx)


async def test_load_mcp_tool_missing_client():
    ctx = make_runtime_ctx("t_mcp", "p_mcp")
    with pytest.raises(McpUnavailable):
        await load_mcp_tool({"mcp_client_id": "does-not-exist", "remote_tool_name": "x"}, ctx)


async def _noop(tools):
    return object(), tools


# --- humanize_mcp_error: unwrap TaskGroup/ExceptionGroup to the real cause -----------------------

class _FakeResp:
    def __init__(self, code, reason=""):
        self.status_code = code
        self.reason_phrase = reason


class _FakeHTTPStatusError(Exception):
    def __init__(self, code, reason):
        self.response = _FakeResp(code, reason)
        super().__init__(f"Client error '{code} {reason}' for url 'https://x'\nMore info: ...")


def test_humanize_unwraps_exceptiongroup_to_http_status():
    from ros.tools.mcp import humanize_mcp_error

    inner = _FakeHTTPStatusError(401, "Unauthorized")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    msg = humanize_mcp_error(group)
    assert "401 Unauthorized" in msg
    assert "requires authentication" in msg  # 401/403 hint
    assert "TaskGroup" not in msg


def test_humanize_unwraps_nested_cause():
    from ros.tools.mcp import humanize_mcp_error

    try:
        try:
            raise ConnectionRefusedError("connection refused")
        except ConnectionRefusedError as e:
            raise RuntimeError("wrapper") from e
    except RuntimeError as top:
        msg = humanize_mcp_error(top)
    assert "connection refused" in msg


def test_humanize_plain_error_single_line():
    from ros.tools.mcp import humanize_mcp_error

    msg = humanize_mcp_error(ValueError("boom\nsecond line"))
    assert msg == "boom"  # collapsed to first line, no wrapper


async def test_get_tools_wraps_connect_failure_as_mcp_unavailable():
    # The centralized _get_tools turns any opaque get_tools() failure into a humanized McpUnavailable,
    # so every caller (discovery, agent attach, runtime) gets the real cause — not a TaskGroup string.
    from ros.tools.mcp import McpUnavailable, _get_tools

    class _Client:
        async def get_tools(self):
            raise ExceptionGroup("unhandled errors in a TaskGroup", [_FakeHTTPStatusError(401, "Unauthorized")])

    with pytest.raises(McpUnavailable) as ei:
        await _get_tools(_Client(), "sg")
    assert "401 Unauthorized" in str(ei.value)
    assert "TaskGroup" not in str(ei.value)


async def test_get_tools_does_not_swallow_cancellation():
    import asyncio as _a

    from ros.tools.mcp import _get_tools

    class _Client:
        async def get_tools(self):
            raise _a.CancelledError()

    with pytest.raises(_a.CancelledError):
        await _get_tools(_Client(), "sg")
