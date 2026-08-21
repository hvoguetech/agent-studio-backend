"""MCP tool kind - consume tools from an external MCP server.

Wiring the `mcp` kind unlocks the whole MCP connector ecosystem (GitHub, Slack,
Postgres, Stripe, filesystem, …) without hand-writing each integration. An `McpClient`
row describes the server (http/sse/stdio transport); a tool's config names the
`remote_tool_name` to expose and any `inject_context` keys to fill from the per-user
runtime context (so the model never sets secrets like user_id/api_key).

MCP discovery is async, so MCP tools are loaded by `load_mcp_tools` from the runtime
assembler (not the sync `materialize_tool` path).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from langchain.tools import ToolRuntime
from sqlalchemy import select

from ros.db.base import SessionLocal
from ros.models import McpClient
from ros.secrets.store import SecretStore

log = logging.getLogger("ros.mcp")

# Cache MultiServerMCPClient instances per mcp_client_id with a TTL so a dead connection or
# an edited server config is eventually re-established without a process restart (audit F12).
# `invalidate_client` drops one entry immediately (called when the McpClient row changes);
# `close_all` is called on shutdown.
_CLIENT_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300.0  # seconds


def invalidate_client(client_id: str) -> None:
    """Drop a cached MCP client so the next run reconnects with the latest config."""
    _CLIENT_CACHE.pop(client_id, None)


def humanize_mcp_error(exc: BaseException) -> str:
    """Turn an MCP connect failure into a readable one-line message.

    langchain-mcp-adapters runs the connection in an anyio TaskGroup, so a real failure (e.g. a
    401 from the server) arrives wrapped in an ExceptionGroup whose own str() is the useless
    'unhandled errors in a TaskGroup (1 sub-exception)'. Unwrap to the innermost concrete cause and
    render it — surfacing an HTTP status when present (httpx.HTTPStatusError) so the UI can say
    '401 Unauthorized' instead of 'TaskGroup'."""
    seen: set[int] = set()

    def _leaf(e: BaseException) -> BaseException:
        # Descend ExceptionGroups (take the first sub) and __cause__/__context__ chains to the leaf.
        while True:
            if id(e) in seen:
                return e
            seen.add(id(e))
            subs = getattr(e, "exceptions", None)  # ExceptionGroup / BaseExceptionGroup
            if subs:
                e = subs[0]
                continue
            nxt = e.__cause__ or e.__context__
            if nxt is not None and id(nxt) not in seen:
                e = nxt
                continue
            return e

    leaf = _leaf(exc)
    # httpx.HTTPStatusError carries a response with a status code — the most useful thing to show.
    resp = getattr(leaf, "response", None)
    status_code = getattr(resp, "status_code", None)
    if isinstance(status_code, int):
        reason = getattr(resp, "reason_phrase", "") or ""
        hint = " — the server requires authentication (add a bearer token)" if status_code in (401, 403) else ""
        return f"{status_code} {reason}".strip() + hint
    msg = str(leaf).strip() or type(leaf).__name__
    # Keep it to a single line; collapse any multi-line httpx message.
    return msg.splitlines()[0]


async def close_all() -> None:
    """Best-effort close of every cached MCP client (transports/subprocesses) on shutdown."""
    for _, client in list(_CLIENT_CACHE.values()):
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
    _CLIENT_CACHE.clear()


class McpUnavailable(RuntimeError):
    pass


def _require_adapters():
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as e:  # pragma: no cover - optional extra
        raise McpUnavailable(
            "mcp tools need `langchain-mcp-adapters` (pip install -e '.[mcp]')."
        ) from e
    return MultiServerMCPClient


async def _validate_mcp_url(url: str | None) -> None:
    """Screen an external MCP server URL through the SSRF egress guard before connecting.
    REST/GraphQL/SQL tools already do this; the MCP client URL was handed straight to the
    transport, so a project editor could point it at 169.254.169.254 or an internal service
    (and any secret headers would be sent there). Enforce the same default-deny here."""
    from ros.util.ssrf import EgressPolicy, validate_url

    if not url:
        raise McpUnavailable("MCP server URL is required for http/sse transport")
    await validate_url(url, EgressPolicy.from_settings())


async def _connection_for(client_row: McpClient, tenant_id: str, project_id: str) -> dict:
    from ros.config import settings

    transport = client_row.transport or "streamable_http"
    if transport in ("http", "streamable_http"):
        await _validate_mcp_url(client_row.url)
        conn: dict[str, Any] = {"url": client_row.url, "transport": "streamable_http"}
    elif transport == "sse":
        await _validate_mcp_url(client_row.url)
        conn = {"url": client_row.url, "transport": "sse"}
    elif transport == "stdio":
        # stdio launches a LOCAL PROCESS -> arbitrary command execution on the API host. Gate it
        # behind an explicit deployment flag (default off, so it can't be enabled by any editor in
        # a multi-tenant install) and an optional command allow-list.
        if not settings.enable_mcp_stdio:
            raise McpUnavailable(
                "MCP stdio transport is disabled. It launches a local process (arbitrary command "
                "execution); enable ROS_ENABLE_MCP_STDIO=true only on a trusted single-tenant install."
            )
        allowed = settings.mcp_stdio_allowed_commands
        if allowed and (client_row.command or "") not in allowed:
            raise McpUnavailable(f"MCP stdio command {client_row.command!r} is not in the allowed list.")
        args = client_row.args or {}
        conn = {"command": client_row.command, "args": args.get("args", []) if isinstance(args, dict) else args, "transport": "stdio"}
    else:
        raise McpUnavailable(f"unsupported MCP transport {transport!r}")
    if client_row.headers_ref:
        try:
            headers = await SecretStore().read_ref(tenant_id=tenant_id, project_id=project_id, ref=client_row.headers_ref)
            if isinstance(headers, dict):
                conn["headers"] = headers
        except Exception:  # noqa: BLE001 - missing headers secret => connect without
            pass
    return conn


def _to_sdk_mcp_config(conn: dict) -> dict:
    """Translate a langchain-mcp-adapters connection dict (from `_connection_for`) into the Claude
    Agent SDK's `mcp_servers` config shape. The SDK keys the transport under `type`
    ("http"/"sse"/"stdio") where the adapters use `transport` ("streamable_http"/"sse"/"stdio")."""
    transport = conn.get("transport")
    if transport in ("http", "streamable_http"):
        cfg: dict[str, Any] = {"type": "http", "url": conn.get("url")}
    elif transport == "sse":
        cfg = {"type": "sse", "url": conn.get("url")}
    elif transport == "stdio":
        cfg = {"type": "stdio", "command": conn.get("command"), "args": conn.get("args") or []}
    else:
        raise McpUnavailable(f"unsupported MCP transport {transport!r}")
    if conn.get("headers"):
        cfg["headers"] = conn["headers"]
    return cfg


async def sdk_server_config(client_row: McpClient, tenant_id: str, project_id: str) -> dict:
    """Resolve a McpClient row to a Claude Agent SDK `mcp_servers` config entry — creds resolved and
    the SSRF / stdio gating enforced via `_connection_for`. Used by the claude_code node, whose CLI
    subprocess connects to the server itself (separate from the in-process langchain-mcp tools that
    agent/deep_agent nodes attach via `server_tools`)."""
    conn = await _connection_for(client_row, tenant_id, project_id)
    return _to_sdk_mcp_config(conn)


async def _client_and_tools(client_row: McpClient, tenant_id: str, project_id: str):
    MultiServerMCPClient = _require_adapters()
    now = time.monotonic()
    entry = _CLIENT_CACHE.get(client_row.id)
    if entry is None or (now - entry[0]) > _CACHE_TTL:
        conn = await _connection_for(client_row, tenant_id, project_id)
        client = MultiServerMCPClient({client_row.name: conn})
        _CLIENT_CACHE[client_row.id] = (now, client)
    else:
        client = entry[1]
    tools = await _get_tools(client, client_row.name)
    return client, tools


async def _get_tools(client, server_name: str):
    """`client.get_tools()` but any anyio TaskGroup/ExceptionGroup connect failure (401, DNS, TLS,
    refused, …) is re-raised as McpUnavailable with the real cause unwrapped — so every caller
    (discovery UI, agent/deep_agent tool attach, claude_code) gets one clear message instead of the
    opaque 'unhandled errors in a TaskGroup'. Cancellation is never swallowed."""
    try:
        return await client.get_tools()
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except McpUnavailable:
        raise
    except BaseException as e:  # noqa: BLE001 - normalize opaque MCP connect failures
        raise McpUnavailable(f"MCP server {server_name!r}: {humanize_mcp_error(e)}") from e


async def discover_tools(client_row: McpClient, tenant_id: str, project_id: str) -> list[dict]:
    """List the tools an MCP server exposes - [{name, description}].

    Connects fresh (not via the execution cache) so the result always reflects the
    current McpClient config, and drops any stale cached client so the next run
    reconnects with the latest settings. Raises McpUnavailable / connection errors.
    """
    _CLIENT_CACHE.pop(client_row.id, None)
    MultiServerMCPClient = _require_adapters()
    conn = await _connection_for(client_row, tenant_id, project_id)
    client = MultiServerMCPClient({client_row.name: conn})
    tools = await _get_tools(client, client_row.name)
    return [{"name": t.name, "description": (getattr(t, "description", "") or "").strip()} for t in tools]


async def server_tools(client_row: McpClient, tenant_id: str, project_id: str) -> list:
    """Native LangChain tools a server exposes, minus the ones toggled off (disabled_tools).
    Used to attach a whole MCP server's tools to an agent."""
    _client, tools = await _client_and_tools(client_row, tenant_id, project_id)
    disabled = set(getattr(client_row, "disabled_tools", None) or [])
    return [t for t in tools if t.name not in disabled]


def _wrap_with_context_injection(tool, inject_keys: list[str]):
    """Wrap an MCP StructuredTool so `inject_keys` are filled from runtime.context
    (per-user secrets the widget/channel supplies) instead of from the model."""
    from langchain_core.tools import StructuredTool

    underlying = tool

    async def _call(runtime: ToolRuntime = None, **kwargs):  # type: ignore[assignment]
        context = getattr(runtime, "context", None) or {}
        for k in inject_keys or []:
            if k in context:
                kwargs[k] = context[k]
        return await underlying.ainvoke(kwargs)

    return StructuredTool.from_function(
        coroutine=_call, name=underlying.name, description=underlying.description,
        args_schema=underlying.args_schema,
    )


async def load_mcp_tool(cfg: dict, ctx) -> Any:
    """Resolve a single `mcp`-kind tool config to a runnable tool (async)."""
    async with SessionLocal() as s:
        row = (
            await s.execute(
                select(McpClient).where(
                    McpClient.tenant_id == ctx.tenant_id, McpClient.id == cfg["mcp_client_id"]
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise McpUnavailable(f"MCP client {cfg.get('mcp_client_id')!r} not found")
    _client, tools = await _client_and_tools(row, ctx.tenant_id, ctx.project_id)
    name = cfg["remote_tool_name"]
    match = next((t for t in tools if t.name == name), None)
    if match is None:
        raise McpUnavailable(f"remote tool {name!r} not exposed by MCP server {row.name!r}")
    inject = cfg.get("inject_context") or []
    return _wrap_with_context_injection(match, inject) if inject else match


def build_mcp_tool(cfg: dict, ctx):
    # MCP discovery is async; the runtime assembler calls load_mcp_tool instead.
    raise McpUnavailable("mcp tools are loaded asynchronously via load_mcp_tool (runtime assembler).")
