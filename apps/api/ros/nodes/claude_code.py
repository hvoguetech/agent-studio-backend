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


def _resolve_cwd(config: dict, workflow_id: str | None, node_id: str | None) -> str:
    """Working directory Claude Code operates in — a STABLE per-node dir so a run's files land
    predictably and a stateful repo agent keeps its working tree across runs.

    Precedence:
      1. explicit `config.workspace` (absolute path) — honored verbatim (power-user pin).
      2. `<base>/<workflow_id>/<node_id>` — the stable per-node dir. `base` is ROS_CLAUDE_CODE_WORKSPACE
         (the per-VM root the runtime sets) else a temp root. `workflow_id` is required because node
         ids are only unique WITHIN a workflow.
      3. a per-node temp dir — fallback when the ids aren't available (ad-hoc/unit-test compile).
    Created if missing; kept absolute so the SDK/CLI never resolves it against an ambiguous cwd."""
    import tempfile

    explicit = (config.get("workspace") or "").strip()
    if explicit:
        workspace = explicit
    elif workflow_id and node_id:
        base = os.environ.get("ROS_CLAUDE_CODE_WORKSPACE", "").strip() or os.path.join(
            tempfile.gettempdir(), "ros-claude-code"
        )
        workspace = os.path.join(base, workflow_id, node_id)
    else:
        workspace = tempfile.mkdtemp(prefix="ros-claude-code-")
    workspace = os.path.abspath(workspace)
    os.makedirs(workspace, exist_ok=True)
    return workspace


def _scrub_token(text: str) -> str:
    """Redact any `x-access-token:<tok>@` embedded in a string (clone URL) so a token never leaks
    into an error message / log."""
    import re

    return re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text or "")


async def _resolve_repo_token(ctx: CompileContext, secret_ref: str) -> str | None:
    """Resolve a `secret://proj/<name>` ref to a GitHub token, scoped to the run's tenant/project.
    Returns None if unresolved (caller falls back to a public clone / fails the clone). The value may
    be a bare string or a structured secret; we take a string or its common token-bearing fields.

    Uses the ctx auth_resolver's secret store so BOTH runtimes work: the trusted VM's store reads
    master DB, while the isolating sandbox's InMemorySecretStore reads the manifest's run-scoped
    secrets (master must include `repo_secret_ref` there, else this fails closed -> None). Falls back
    to a direct SecretStore when no resolver is attached (e.g. master/dev)."""
    if not secret_ref:
        return None
    from ros.secrets.store import SecretNotFound, SecretStore

    resolver = getattr(ctx, "auth_resolver", None)
    store = getattr(resolver, "secrets", None) or SecretStore()
    try:
        val = await store.read_ref(tenant_id=ctx.tenant_id, project_id=ctx.project_id, ref=secret_ref)
    except SecretNotFound:
        return None
    if isinstance(val, str):
        return val or None
    if isinstance(val, dict):
        for k in ("token", "value", "password", "pat"):
            if isinstance(val.get(k), str) and val[k]:
                return val[k]
    return None


def _clone_url(repo_url: str, token: str | None) -> str:
    """Splice a token into an https git URL as an x-access-token (GitHub's app-token scheme), so the
    token is never stored in the repo URL at rest — mirrors the image bake's clone step."""
    if token and repo_url.startswith("https://"):
        return "https://x-access-token:" + token + "@" + repo_url[len("https://"):]
    return repo_url


async def _checkout_repo(config: dict, ctx: CompileContext, cwd: str) -> None:
    """Clone-once: if `repo_url` is set and the workspace has no `.git`, shallow-clone the configured
    branch/tag into it. If the repo is already present, leave the working tree AS-IS (the agent keeps
    prior-run changes). Runs the `git` subprocess in the VM; token (if any) is used only to build the
    clone URL and is scrubbed from any error surfaced."""
    repo_url = (config.get("repo_url") or "").strip()
    if not repo_url:
        return
    if os.path.isdir(os.path.join(cwd, ".git")):
        return  # clone-once: keep the existing working tree

    token = await _resolve_repo_token(ctx, (config.get("repo_secret_ref") or "").strip())
    url = _clone_url(repo_url, token)
    ref = (config.get("repo_ref") or "").strip()

    import asyncio

    args = ["git", "clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]  # branch or tag (v1; no arbitrary SHA)
    args += [url, cwd]

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = _scrub_token((stderr or b"").decode("utf-8", "replace").strip())
        raise ClaudeCodeError(
            f"claude_code repo checkout failed for {repo_url}"
            + (f" (ref {ref})" if ref else "")
            + (f": {detail}" if detail else ""),
            stderr=detail or None,
        )


def _sdk_options(config: dict, cwd: str, stderr_sink, env: dict[str, str], mcp_servers: dict[str, dict]):
    """Build ClaudeAgentOptions from node config. Imported lazily so the core never needs the SDK
    unless a workflow actually uses this node.

    `stderr_sink` is the SDK's per-line stderr callback: the real subprocess error detail (the CLI's
    "Check stderr output for details" placeholder refers to *this* stream) only reaches us here, so
    we always wire it up to buffer stderr for error reporting.

    `env` is the per-call subprocess env overlay (e.g. the governed ANTHROPIC_API_KEY). The SDK merges
    it over os.environ for the spawned CLI only (transport: {**os.environ, ..., **options.env}), so the
    key is scoped to this one subprocess — never mutated onto the shared process env, which would race
    across concurrent runs on the same event loop."""
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stderr": stderr_sink,
        "env": env,
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
    if config.get("fallback_model"):
        # Secondary model tried if the primary errors mid-run.
        kwargs["fallback_model"] = config["fallback_model"]
    if config.get("max_budget_usd") is not None:
        # Hard USD cost cap on the whole loop; the SDK aborts when accrued cost crosses it. Parallel
        # to max_turns and the node-level tenant_budget hard-cap — a cost ceiling, not a turn ceiling.
        kwargs["max_budget_usd"] = float(config["max_budget_usd"])
    if config.get("effort"):
        # Reasoning-effort control: "low" | "medium" | "high" | "xhigh" | "max".
        kwargs["effort"] = config["effort"]
    thinking = (config.get("thinking") or "").strip()
    if thinking == "adaptive":
        kwargs["thinking"] = {"type": "adaptive"}
    elif thinking == "disabled":
        kwargs["thinking"] = {"type": "disabled"}
    elif thinking == "enabled":
        # budget_tokens is required for the enabled variant; default when the UI leaves it blank.
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": int(config.get("thinking_budget_tokens") or 8000)}
    if config.get("allowed_tools") is not None:
        kwargs["allowed_tools"] = list(config["allowed_tools"])
    if config.get("disallowed_tools") is not None:
        kwargs["disallowed_tools"] = list(config["disallowed_tools"])
    if config.get("setting_sources") is not None:
        # Whether the CLI loads user/project/local settings & CLAUDE.md. Default: none (hermetic).
        kwargs["setting_sources"] = list(config["setting_sources"])
    if mcp_servers:
        # SDK mcp_servers: name -> server config. The CLI subprocess connects to these itself.
        kwargs["mcp_servers"] = mcp_servers
    return ClaudeAgentOptions(**kwargs)


def _inject_anthropic_key(ctx: CompileContext) -> dict[str, str]:
    """The SDK's CLI subprocess reads ANTHROPIC_API_KEY from its env. Surface the project's governed
    key (resolved by the runtime assembler into ctx.provider_credentials, same path as resolve_model)
    so the node uses the same credential as every other Anthropic call — never a hard-coded key.
    Returns the env overlay passed to ClaudeAgentOptions.env; the SDK merges it over os.environ for the
    spawned CLI only (never onto the shared process env)."""
    creds = getattr(ctx, "provider_credentials", None) or {}
    key = creds.get("anthropic")
    return {"ANTHROPIC_API_KEY": key} if key else {}


def _resolve_mcp_servers(config: dict, ctx: CompileContext) -> dict[str, dict]:
    """Build the SDK `mcp_servers` mapping from the node's selected mcp_client ids.

    The runtime assembler pre-resolved each project MCP server to an SDK-shaped config (creds +
    SSRF/stdio gating applied) into `ctx.mcp_server_configs` (keyed by client id -> {name, server}).
    Here we pick only the ids the node opted into (config.mcp_servers) and key them by human name for
    the CLI. Ids the ctx couldn't resolve (unavailable / disabled) are skipped, not errored. This is
    the CLI subprocess's own connect — it does NOT touch the in-process tools agent nodes use."""
    ids = config.get("mcp_servers") or []
    resolved = getattr(ctx, "mcp_server_configs", None) or {}
    out: dict[str, dict] = {}
    for cid in ids:
        entry = resolved.get(cid)
        if entry and entry.get("server"):
            out[str(entry.get("name") or cid)] = entry["server"]
    return out


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


def claude_code_factory(config: dict, ctx: CompileContext, node_id: str | None = None):
    key_env = _inject_anthropic_key(ctx)
    mcp_servers = _resolve_mcp_servers(config, ctx)
    workflow_id = getattr(ctx, "workflow_id", None)

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

        # Resolve the workspace per invocation (so it keys on workflow+node and picks up the runtime's
        # ROS_CLAUDE_CODE_WORKSPACE without a recompile), then clone-once any configured repo into it.
        cwd = _resolve_cwd(config, workflow_id, node_id)
        await _checkout_repo(config, ctx, cwd)

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

        # Scope the governed key to this subprocess via options.env — never mutate the shared process
        # env, which would race across concurrent runs on the same event loop.
        options = _sdk_options(config, cwd, _stderr_sink, key_env, mcp_servers)
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
    ws = config.get("workspace") or "per-node workspace"
    lines = [str(model), f"{mode} · {ws}"]
    repo = (config.get("repo_url") or "").strip()
    if repo:
        ref = (config.get("repo_ref") or "").strip()
        lines.append(f"repo: {repo}" + (f"@{ref}" if ref else ""))
    n_mcp = len(config.get("mcp_servers") or [])
    if n_mcp:
        lines.append(f"MCP {n_mcp}")
    return lines


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
