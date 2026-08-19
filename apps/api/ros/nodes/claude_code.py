"""Claude Code node — wrap the Claude Agent SDK as a LangGraph node.

The Claude Agent SDK (`claude-agent-sdk`) drives Anthropic's `claude` CLI as a subprocess:
it runs an autonomous agent loop with real file / shell / edit tools inside a working
directory. This node adapts that loop to the ROS node contract (registry.py): a factory
`(config, ctx) -> callable(state) -> dict` whose callable reads the inbound `messages`
channel, runs Claude Code to completion in a cwd, and writes the final assistant text back
onto `messages` (with cost/usage surfaced in the returned state for the tracer/meters).

Isolation note (see docs/GAPS.md#g1 and docs/design/secure-multitenant-execution.md): the SDK does
real OS work in `cwd`. On the master/control plane this is the trusted-single-tenant dev path; the
hardened multi-tenant path is to run this node inside the per-run VM (ros.runtime on the Freestyle/
E2B execution backend), where the process owns exactly one run. The node itself is transport-only —
it does not weaken any egress/authz boundary; those stay where the run executes. Hard multi-tenant
isolation is an OPEN GAP (G1) pending the `sandbox` execution backend — do not expose this node to
untrusted multi-tenant callers until then.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ros.engine.context import CompileContext
from ros.engine.registry import NodeSpec, Port, register

log = logging.getLogger("ros.claude_code")

# Turn cap so a runaway agent loop can't burn tokens forever. Overridable via config.
_DEFAULT_MAX_TURNS = 40


def _last_human_text(messages: list[BaseMessage]) -> str:
    """The prompt for Claude Code = the most recent human/user message text. Falls back to the
    last message of any role so an upstream node that emits an AIMessage still drives it."""
    for msg in reversed(messages or []):
        if isinstance(msg, HumanMessage):
            return _text_of(msg)
    for msg in reversed(messages or []):
        text = _text_of(msg)
        if text:
            return text
    return ""


def _text_of(msg: BaseMessage) -> str:
    """Flatten a message's content to plain text (str content, or the text parts of a list)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _resolve_cwd(config: dict) -> str:
    """Working directory Claude Code operates in.

    Precedence: explicit config.workspace > ROS_CLAUDE_CODE_WORKSPACE env (set by the runtime on a
    VM) > a per-node temp dir. Created if missing. Kept absolute so the SDK/CLI never resolves it
    against an ambiguous cwd."""
    workspace = (config.get("workspace") or "").strip() or os.environ.get("ROS_CLAUDE_CODE_WORKSPACE", "")
    if not workspace:
        import tempfile

        workspace = tempfile.mkdtemp(prefix="ros-claude-code-")
    workspace = os.path.abspath(workspace)
    os.makedirs(workspace, exist_ok=True)
    return workspace


def _sdk_options(config: dict, cwd: str):
    """Build ClaudeAgentOptions from node config. Imported lazily so the core never needs the SDK
    unless a workflow actually uses this node."""
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, Any] = {
        "cwd": cwd,
        # "default" | "acceptEdits" | "bypassPermissions" | "plan". Autonomous runs (no human at the
        # keyboard) need edits to not block on a prompt; default to acceptEdits, overridable.
        "permission_mode": config.get("permission_mode", "acceptEdits"),
        "max_turns": int(config.get("max_turns") or _DEFAULT_MAX_TURNS),
    }
    if config.get("system_prompt"):
        kwargs["system_prompt"] = config["system_prompt"]
    if config.get("model"):
        # Bare Anthropic model id (e.g. "claude-sonnet-4-5"), NOT the "provider:model" ROS form.
        kwargs["model"] = config["model"]
    if config.get("allowed_tools") is not None:
        kwargs["allowed_tools"] = list(config["allowed_tools"])
    if config.get("disallowed_tools") is not None:
        kwargs["disallowed_tools"] = list(config["disallowed_tools"])
    if config.get("setting_sources") is not None:
        # Whether the CLI loads user/project/local settings & CLAUDE.md. Default: none (hermetic).
        kwargs["setting_sources"] = list(config["setting_sources"])
    return ClaudeAgentOptions(**kwargs)


def _inject_anthropic_key(ctx: CompileContext) -> dict[str, str]:
    """The SDK's CLI subprocess reads ANTHROPIC_API_KEY from its env. Surface the project's governed
    key (resolved by the runtime assembler into ctx.provider_credentials, same path as resolve_model)
    so the node uses the same credential as every other Anthropic call — never a hard-coded key.
    Returns the env overlay to pass to the subprocess (the SDK forwards os.environ + this)."""
    creds = getattr(ctx, "provider_credentials", None) or {}
    key = creds.get("anthropic")
    return {"ANTHROPIC_API_KEY": key} if key else {}


def claude_code_factory(config: dict, ctx: CompileContext):
    cwd = _resolve_cwd(config)
    key_env = _inject_anthropic_key(ctx)

    async def _node(state: dict) -> dict:
        try:
            from claude_agent_sdk import query
        except ImportError as e:  # pragma: no cover - optional extra
            raise ImportError(
                "The claude_code node needs the Claude Agent SDK: install the extra with "
                "`pip install -e '.[claude_code]'` (also requires the `claude` CLI + Node on PATH)."
            ) from e

        prompt = _last_human_text(state.get("messages") or [])
        if not prompt:
            return {"messages": [AIMessage(content="(claude_code: no input prompt)")]}

        # The SDK spawns the `claude` CLI inheriting os.environ; overlay the governed key for the
        # duration of this call without leaking it into the long-lived process env.
        restore: dict[str, str | None] = {}
        for k, v in key_env.items():
            restore[k] = os.environ.get(k)
            os.environ[k] = v

        text_chunks: list[str] = []
        usage: dict[str, Any] = {}
        cost_usd: float | None = None
        options = _sdk_options(config, cwd)
        try:
            async for message in query(prompt=prompt, options=options):
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    # AssistantMessage.content is a list of blocks (TextBlock / ToolUseBlock / ...).
                    for block in getattr(message, "content", []) or []:
                        if type(block).__name__ == "TextBlock":
                            text_chunks.append(getattr(block, "text", "") or "")
                elif kind == "ResultMessage":
                    # Terminal frame: carries the final result text + cost/usage for the whole loop.
                    result_text = getattr(message, "result", None)
                    if result_text:
                        text_chunks = [result_text]  # authoritative final answer; supersede streamed bits
                    cost_usd = getattr(message, "total_cost_usd", None)
                    usage = getattr(message, "usage", None) or {}
                    if getattr(message, "is_error", False):
                        log.warning("claude_code run reported is_error=True: %s", result_text)
        finally:
            for k, prev in restore.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev

        final = "\n".join(c for c in text_chunks if c).strip() or "(claude_code: empty result)"
        out = AIMessage(
            content=final,
            # Surface usage/cost so the ROS tracer/cost meter can attribute this node's spend.
            response_metadata={"claude_code": {"cost_usd": cost_usd, "usage": usage, "cwd": cwd}},
        )
        return {"messages": [out]}

    return _node


def _summary(config: dict) -> list[str]:
    model = config.get("model") or "claude (default)"
    mode = config.get("permission_mode", "acceptEdits")
    ws = config.get("workspace") or "runtime workspace"
    return [str(model), f"{mode} · {ws}"]


register(
    NodeSpec(
        type="claude_code",
        schema_id="ros/nodes/claude_code",
        input_ports=[Port(id="in", io_type="messages", direction="in")],
        output_ports=[Port(id="out", io_type="messages", direction="out")],
        factory=claude_code_factory,
        allows_cycle=True,
        category="agents",
        label="Claude Code",
        description="Claude Agent SDK (autonomous file/shell agent) in a workspace",
        summarize=_summary,
    )
)
