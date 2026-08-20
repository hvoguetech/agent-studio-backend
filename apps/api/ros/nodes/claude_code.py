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

# Cap on captured subprocess stderr so a chatty CLI can't blow up the error message / memory.
_STDERR_CAP_BYTES = 16_384


class ClaudeCodeError(RuntimeError):
    """Raised when the Claude Code run fails, carrying the exact CLI/subprocess error.

    The Claude Agent SDK's own "Command failed with exit code 1 ... Check stderr output for
    details" text is a placeholder — the real detail is on the subprocess stderr and on the
    terminal `ResultMessage` (result prose, `errors`, `subtype`, `api_error_status`). This node
    captures both and surfaces them here instead of swallowing them, so callers see *why* the run
    failed rather than the placeholder.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str | None = None,
        subtype: str | None = None,
        errors: list[str] | None = None,
        api_error_status: int | None = None,
    ):
        self.exit_code = exit_code
        self.stderr = stderr
        self.subtype = subtype
        self.errors = errors or []
        self.api_error_status = api_error_status
        super().__init__(message)


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


def _sdk_options(config: dict, cwd: str, stderr_sink):
    """Build ClaudeAgentOptions from node config. Imported lazily so the core never needs the SDK
    unless a workflow actually uses this node.

    `stderr_sink` is the SDK's per-line stderr callback: the real subprocess error detail (the CLI's
    "Check stderr output for details" placeholder refers to *this* stream) only reaches us here, so
    we always wire it up to buffer stderr for error reporting."""
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stderr": stderr_sink,
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


def _join_stderr(stderr_lines: list[str]) -> str:
    """Collapse the buffered stderr lines to a single trimmed string (empty if none captured)."""
    return "".join(stderr_lines).strip()


def _result_error(message: Any, stderr_lines: list[str]) -> ClaudeCodeError:
    """Build a ClaudeCodeError from a terminal ResultMessage with is_error=True.

    Pulls the CLI's structured failure fields (result prose, `errors`, `subtype`, `api_error_status`)
    and appends the captured subprocess stderr so the exact detail is in the message, not a placeholder."""
    result_text = getattr(message, "result", None) or ""
    subtype = getattr(message, "subtype", None)
    errors = getattr(message, "errors", None) or []
    api_error_status = getattr(message, "api_error_status", None)
    stderr = _join_stderr(stderr_lines)

    parts: list[str] = ["claude_code run failed"]
    if subtype and subtype != "success":
        parts.append(f"({subtype})")
    if api_error_status:
        parts.append(f"[api status {api_error_status}]")
    detail = result_text.strip() or "; ".join(str(e) for e in errors) or "(no result text)"
    msg = f"{' '.join(parts)}: {detail}"
    if stderr and stderr not in detail:
        msg = f"{msg}\nstderr:\n{stderr}"
    return ClaudeCodeError(
        msg,
        subtype=subtype,
        errors=[str(e) for e in errors],
        api_error_status=api_error_status,
        stderr=stderr or None,
    )


def _wrap_sdk_error(exc: Exception, stderr_lines: list[str]) -> Exception:
    """Normalize whatever propagated out of the SDK loop into a detailed error.

    - A ClaudeCodeError we already raised (from is_error) passes through unchanged.
    - The SDK's ProcessError/ResultError carry `.exit_code`/`.stderr` (and, for ResultError,
      `.subtype`/`.errors`/`.api_error_status`); we fold those plus the captured stderr into a
      ClaudeCodeError so nothing is lost.
    - ImportError (missing SDK) and any other exception pass through unchanged."""
    if isinstance(exc, (ClaudeCodeError, ImportError)):
        return exc

    exit_code = getattr(exc, "exit_code", None)
    # Prefer the exception's own stderr (ProcessError.stderr) but fall back to our captured buffer,
    # which is populated even when the SDK left `.stderr` as the "Check stderr output" placeholder.
    exc_stderr = getattr(exc, "stderr", None)
    captured = _join_stderr(stderr_lines)
    stderr = "\n".join(s for s in (exc_stderr, captured) if s and s not in (exc_stderr or "")) or exc_stderr or captured

    msg = f"claude_code run failed: {exc}"
    if stderr and stderr not in str(exc):
        msg = f"{msg}\nstderr:\n{stderr}"
    return ClaudeCodeError(
        msg,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        stderr=stderr or None,
        subtype=getattr(exc, "subtype", None),
        errors=[str(e) for e in (getattr(exc, "errors", None) or [])],
        api_error_status=getattr(exc, "api_error_status", None),
    )


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
        # Buffer the subprocess's stderr; this is where the real failure detail lands.
        stderr_lines: list[str] = []
        stderr_bytes = 0

        def _stderr_sink(line: str) -> None:
            nonlocal stderr_bytes
            if stderr_bytes >= _STDERR_CAP_BYTES:
                return
            stderr_bytes += len(line)
            stderr_lines.append(line)

        options = _sdk_options(config, cwd, _stderr_sink)
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
                        # Don't swallow it: surface the exact failure (result prose + errors + subtype
                        # + api status + captured stderr) so callers see why the run failed.
                        raise _result_error(message, stderr_lines)
        except Exception as e:  # noqa: BLE001 - normalize SDK/CLI failures to a detailed ClaudeCodeError
            raise _wrap_sdk_error(e, stderr_lines) from e
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
