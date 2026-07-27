"""Agent node (`create_agent`) and Deep Agent node (`create_deep_agent`).

Doc 2 §9. Both produce a compiled LangGraph graph used as a node inside the
workflow. The embedded agent does NOT carry its own checkpointer/store - the
top-level workflow graph owns durability, and LangGraph propagates it to subgraphs
at runtime (avoids nested-checkpointer conflicts and makes HITL interrupts bubble up).
"""

from __future__ import annotations

import logging
from typing import Any

from forge.engine.context import CompileContext
from forge.engine.middleware_compiler import build_middleware
from forge.engine.models import resolve_model
from forge.engine.registry import NodeSpec, Port, register

log = logging.getLogger("forge.agent")


def _dedup_tools_by_name(tools: list) -> list:
    """Bind each tool NAME to the model at most once. `resolve_tool_ids` already de-dups by id (a
    tool shared across several sets is ONE record → one id, sent once), but tool names are NOT
    unique per project and the final list mixes sources (tools + knowledge + MCP + components), so
    two entries can still collide by name - which providers reject (OpenAI errors on a duplicate
    function name). Keep the first occurrence; drop later name-collisions with a warning."""
    seen: set[str] = set()
    out: list = []
    for t in tools:
        name = getattr(t, "name", None)
        if name is not None and name in seen:
            log.warning("agent tool name %r appears more than once; keeping the first, dropping the rest", name)
            continue
        if name is not None:
            seen.add(name)
        out.append(t)
    return out

# Forge's default output style: every agent reply renders as GitHub-Flavored Markdown
# (Feature 1 - structured responses). It lives in the system prompt, so it costs ~nothing
# per turn (and is cached by the Anthropic prompt-caching middleware). Opt out with
# config output_style="plain"; auto-skipped for structured-output agents (they emit JSON).
OUTPUT_STYLE = (
    "Format every reply as GitHub-Flavored Markdown so it renders cleanly: short "
    "paragraphs; `##`/`###` headings for sections; `-` or numbered lists; GFM tables for "
    "comparisons or structured data; fenced code blocks with a language hint for code; and "
    "**bold** for key terms. Keep the structure minimal - only as much as the answer needs "
    "- and never output raw HTML."
)


# When UI components are attached, structured data should be shown via a component (table/
# card/form), NOT a markdown table - so this variant drops the "GFM tables for structured
# data" clause to avoid competing with the widgets (audit B1).
OUTPUT_STYLE_WITH_COMPONENTS = (
    "Format every reply as GitHub-Flavored Markdown so it renders cleanly: short paragraphs; "
    "`##`/`###` headings; `-` or numbered lists; fenced code blocks with a language hint; and "
    "**bold** for key terms. For structured data (tables, cards, forms), prefer the available "
    "UI components over a markdown table. Keep structure minimal and never output raw HTML."
)


# Steer the agent to RENDER a fitting component instead of restating its data as prose, and to
# POSITION it correctly: calling a component tool returns a placeholder marker that the agent
# copies into its reply where the widget belongs - so the component is interleaved with the text
# in its natural place (mid-answer, after a heading, at the end) rather than always pinned to the
# top (which is what happens if placement is left to tool-call order). The last sentence is
# load-bearing: it makes clear components only PRESENT data, so the agent keeps using its
# retrieval/other tools normally - without it, the component guidance was competing with
# knowledge/FAQ search and the agent skipped it (audit Priority B + the KB regression). Only
# appended when config["components"] is non-empty.
COMPONENT_STYLE = (
    "You have UI components available as tools (their names match the components). If a "
    "component fits the data you want to show (a table, card, form, …), you MUST call that "
    "component tool with the data as its props INSTEAD of writing the same data as prose or a "
    "markdown table. The tool returns a placeholder marker like [[forge:component:ID]]; copy that "
    "marker verbatim into your reply at the exact position where the component should appear. You "
    "control the order - write text before and after the marker so the component lands in its "
    "natural place in the answer (in the middle, after a heading, or at the end), exactly as it "
    "would read in a normal reply. Never restate the component's contents as text. This governs "
    "only how you PRESENT data - keep using your other tools (search the knowledge base, look up "
    "FAQs, call APIs) normally to GET the information you need."
)


def _build_prompt(config: dict) -> str | None:
    # Static system prompt + Forge's default Markdown output style (+ component guidance when
    # components are attached). Dynamic prompts compile to a middleware (added later).
    base = (config.get("system_prompt") or "").strip()
    structured = (config.get("response_format") or {}).get("mode") == "structured"
    if structured or config.get("output_style") == "plain":
        return base or None
    has_components = bool(config.get("components"))
    style = OUTPUT_STYLE_WITH_COMPONENTS if has_components else OUTPUT_STYLE
    parts = ([base] if base else []) + [style]
    if has_components:
        parts.append(COMPONENT_STYLE)
    return "\n\n".join(parts).strip()


def _build_response_format(config: dict) -> Any:
    rf = config.get("response_format")
    if not rf or rf.get("mode") != "structured":
        return None
    # create_agent accepts a raw JSON-schema dict (auto provider/tool strategy).
    return rf.get("schema")


# deepagents' built-in `task`-tool guidance is a ~536-token essay injected into EVERY supervisor
# call. This concise replacement keeps the essential guidance at ~1/8th the tokens (a real per-turn
# cost lever for supervisors, which re-read their prompt every turn). Override via SubAgentMiddleware.
_TASK_TOOL_PROMPT = (
    "Use the `task` tool to delegate a self-contained subtask to one of the specialist subagents "
    "listed in its schema. Prefer it for multi-step work you can hand off wholesale, and dispatch "
    "independent subagents in parallel. You receive only each subagent's final result — not its "
    "intermediate steps — so give it a clear, complete instruction. Don't delegate trivial one-tool "
    "lookups; do those yourself."
)


def build_subagents(subagents_cfg: list[dict], ctx: CompileContext, default_model: Any = None) -> list[dict]:
    """Convert subagent configs to the SubAgent dict shape SubAgentMiddleware expects.

    Standalone SubAgentMiddleware (our lean deep_agent, see agent_factory) REQUIRES each subagent to
    carry `model` and `tools` - create_deep_agent used to fill those in - and deepagents re-resolves
    a *string* model via init_chat_model WITHOUT our injected provider key, so we always set a
    RESOLVED model object (defaulting to the parent deep_agent's model) and a concrete tools list.
    `system_prompt` is always set (deepagents wraps it). `workflow_ref` subagents are the still-
    unwired subworkflow phase.
    """
    out: list[dict] = []
    for sa in subagents_cfg or []:
        if "workflow_ref" in sa:
            continue  # TODO(phase: subworkflow): wrap compiled workflow as CompiledSubAgent
        name = sa["name"]
        spec: dict[str, Any] = {
            "name": name,
            "description": sa.get("description", ""),
            "system_prompt": sa.get("system_prompt") or sa.get("description") or f"You are the {name} agent.",
            "tools": _dedup_tools_by_name(ctx.tools_for(ctx.resolve_tool_ids(sa.get("tools"), sa.get("toolsets")))),
            "model": resolve_model(sa["model"], ctx) if sa.get("model") else (default_model or resolve_model(None, ctx)),
        }
        if sa.get("middleware"):
            spec["middleware"] = build_middleware(sa["middleware"], ctx)
        out.append(spec)
    return out


def _maybe_add_prompt_caching(stack: list[dict], config: dict, ctx: CompileContext) -> list[dict]:
    """Prepend Anthropic prompt-caching middleware for Anthropic-model agents (cost lever),
    unless already present or disabled. Best-effort: only when langchain-anthropic exists."""
    import importlib.util

    from forge.config import settings

    if not settings.default_anthropic_prompt_caching:
        return stack
    model_ref = config.get("model") or getattr(ctx, "default_model", "") or ""
    if not (isinstance(model_ref, str) and model_ref.startswith("anthropic")):
        return stack
    if any((m or {}).get("type") == "anthropic_prompt_caching" for m in stack):
        return stack
    if importlib.util.find_spec("langchain_anthropic") is None:
        return stack
    return [{"type": "anthropic_prompt_caching", "config": {}}, *stack]


def _clamp(value, n: int = 300):
    """Bound an end_user value's size before it enters the prompt (avoid bloat/abuse)."""
    if isinstance(value, str):
        return value[:n]
    if isinstance(value, list):
        return [_clamp(v, n) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k)[:60]: _clamp(v, n) for k, v in list(value.items())[:20]}
    return value


def _end_user_block(end_user: dict) -> str:
    """A generic identity-awareness block. Only a whitelisted, size-clamped subset of the
    (untrusted-shaped) end_user is embedded, re-serialized so the JSON stays well-formed
    (audit L4). The withhold-restriction sentence is added ONLY when the user actually carries
    roles/entitlements - an unscoped prohibition with no entitlement list made the model
    over-refuse general KB/FAQ answers ("I don't have that information") (audit Priority A)."""
    import json as _json

    safe = {
        k: _clamp(end_user[k])
        for k in ("id", "display_name", "email", "roles", "entitlements", "attributes")
        if end_user.get(k) not in (None, "", [], {})
    }
    if not safe:
        return ""
    eu = _json.dumps(safe, default=str, ensure_ascii=False)
    line = (
        "[END USER] You are assisting this authenticated end user, provided by the host "
        f"application - treat it as authoritative: {eu}."
    )
    if safe.get("roles") or safe.get("entitlements"):
        line += (
            " General product, FAQ, and knowledge-base information is available to everyone - "
            "always answer it. Only withhold data that is specific to OTHER users or accounts "
            "this user is not entitled to see or act on."
        )
    return line


def _dynamic_field_middleware(config: dict, ctx: CompileContext, base_prompt: str | None) -> list:
    """Compile the agent node's `dynamic_model` / `dynamic_prompt` blocks - both exposed in the
    UI but previously unwired (audit F9) - into middleware.

    - dynamic_model reuses the proven `dynamic_model_by_state` builder (switch model by a
      state expression).
    - dynamic_prompt renders the FIRST matching rule's prompt (with `{{state.*}}` tokens) as
      the system prompt per model call, falling back to the node's static prompt when no rule
      matches - so enabling it with no matching rule is behavior-neutral."""
    extra: list = []

    dm = config.get("dynamic_model") or {}
    if dm.get("enabled") and dm.get("rules"):
        extra += build_middleware(
            [{"type": "dynamic_model_by_state", "config": {"rules": dm["rules"], "default": dm.get("default")}}],
            ctx,
        )

    dp = config.get("dynamic_prompt") or {}
    rules = dp.get("rules") or []
    if dp.get("enabled") and rules:
        from langchain.agents.middleware import dynamic_prompt as _dynamic_prompt

        from forge.auth_providers.templates import render_template
        from forge.engine.expressions import ExpressionError, eval_truthy

        fallback = base_prompt or ""

        @_dynamic_prompt
        def _prompt(request):  # type: ignore[no-untyped-def]
            state = dict(getattr(request, "state", {}) or {})
            for r in rules:
                text = r.get("prompt")
                if not text:
                    continue
                when = r.get("when")
                try:
                    if not when or eval_truthy(when, state):
                        rendered = render_template(text, {"state": state}) if isinstance(text, str) else text
                        return str(rendered) if rendered is not None else fallback
                except ExpressionError:
                    continue
            return fallback

        extra.append(_prompt)

    return extra


def _common_kwargs(config: dict, ctx: CompileContext) -> dict:
    tools = list(ctx.tools_for(ctx.resolve_tool_ids(config.get("tools"), config.get("toolsets"))))
    # Built-in knowledge access (RAG / Q&A) attached straight to the agent via its
    # `knowledge` config - no separate Tool row needed (see tools/builtin.py).
    if config.get("knowledge"):
        from forge.tools.builtin import build_knowledge_capability_tools
        tools += build_knowledge_capability_tools(config["knowledge"], ctx)
    # Agent-scoped MCP server access: attach each selected server's enabled tools
    # (pre-loaded by the runtime assembler into ctx.mcp_tools_by_client; native MCP tools).
    for cid in config.get("mcp_servers", []) or []:
        tools += (getattr(ctx, "mcp_tools_by_client", None) or {}).get(cid, [])
    # User-defined UI components exposed as widget-tools (Feature 2): the agent "renders"
    # one by calling it; the client draws the saved template from the props it passes.
    tools += list(ctx.components_for(config.get("components", [])))
    # Final guard: exactly one function name per model call, whatever the source mix.
    tools = _dedup_tools_by_name(tools)
    stack = (ctx.project_default_mw or []) + (config.get("middleware") or [])
    stack = _maybe_add_prompt_caching(stack, config, ctx)
    middleware = build_middleware(stack, ctx)
    model = resolve_model(config.get("model"), ctx, config.get("model_params"))

    common: dict[str, Any] = {"model": model, "tools": tools, "middleware": middleware}
    prompt = _build_prompt(config)
    # Identity awareness: if the run acts for an end user, append a generic context block so
    # the agent knows who it's helping and to stay within their entitlements. Appended last,
    # so the (cacheable) instructions prefix is unchanged; only this per-user suffix varies.
    end_user = getattr(ctx, "end_user", None)
    if end_user:
        eu_block = _end_user_block(end_user)
        if eu_block:
            prompt = f"{prompt}\n\n{eu_block}" if prompt else eu_block
    if prompt:
        common["system_prompt"] = prompt
    # Wire the dynamic_model / dynamic_prompt config blocks (append after the static stack so
    # a matching rule overrides the base at call time). base_prompt = the fully-built static
    # prompt so a non-matching dynamic_prompt run reproduces the static behavior exactly.
    dynamic_mw = _dynamic_field_middleware(config, ctx, prompt)
    if dynamic_mw:
        common["middleware"] = list(middleware) + dynamic_mw
    rf = _build_response_format(config)
    if rf is not None:
        common["response_format"] = rf
    if config.get("name"):
        common["name"] = config["name"]
    return common


def _resolve_config(config: dict, ctx: CompileContext) -> dict:
    """If the node mirrors a saved agent (`agent_ref`), the live preset drives it - so
    edits in the Agents tab take effect without re-saving the workflow. Falls back to the
    node's own (snapshot) config when the preset is missing/unresolved."""
    ref = config.get("agent_ref")
    if ref:
        preset = (getattr(ctx, "agent_presets", None) or {}).get(ref)
        if preset:
            return dict(preset)
    return config


def agent_factory(config: dict, ctx: CompileContext):
    config = _resolve_config(config, ctx)
    common = _common_kwargs(config, ctx)

    from langchain.agents import create_agent

    if config.get("flavor") == "deep_agent":
        # A deep_agent is `create_agent` + only the deepagents harness pieces the operator opts
        # into (planning / filesystem / subagents). We do NOT use `create_deep_agent`, which always
        # bundles the FULL harness (write_todos + filesystem + a general-purpose subagent + a large
        # base prompt) - that overhead made a simple lookup ~6x the tokens of a lean agent. Each
        # piece is added only when its config toggle is on, exactly like attaching a tool.
        try:
            from deepagents import SubAgentMiddleware
            from deepagents.backends import StateBackend
        except ImportError as e:  # pragma: no cover - deepagents is a core dep
            raise ImportError(
                "deep_agent flavor needs `deepagents` (a core dependency - reinstall with "
                "`pip install -e .`)."
            ) from e

        # Backend for the filesystem / subagent middleware: a sandbox if configured, else the
        # default in-memory (thread-scoped) state backend - which carries NO shell `execute` tool.
        backend = ctx.sandbox_backend_for(config.get("sandbox", {}) or {}) or StateBackend()

        middleware = list(common.get("middleware") or [])
        if config.get("planning"):  # write_todos planner (off by default - pure token overhead)
            from langchain.agents.middleware import TodoListMiddleware
            middleware.append(TodoListMiddleware())
        fs = config.get("filesystem") or {}
        if fs.get("enabled") or (fs.get("backend") and fs.get("backend") != "none"):
            from deepagents.middleware import FilesystemMiddleware
            middleware.append(FilesystemMiddleware(backend=backend))
        # Deep-agent skills (agent-skills source paths): wire as SkillsMiddleware - the SAME
        # middleware create_deep_agent uses - so deep_agent NODES keep the skills capability under
        # the lean create_agent path (backend-backed, so skill files load from the configured store).
        if config.get("skills"):
            from deepagents.middleware.skills import SkillsMiddleware
            middleware.append(SkillsMiddleware(backend=backend, sources=list(config["skills"])))
        subagents = build_subagents(config.get("subagents", []), ctx, default_model=common["model"])
        if subagents:
            middleware.append(SubAgentMiddleware(backend=backend, subagents=subagents, system_prompt=_TASK_TOOL_PROMPT))

        kwargs: dict[str, Any] = dict(common)
        kwargs["middleware"] = middleware
        return create_agent(**kwargs)

    return create_agent(**common)


def _summary(config: dict) -> list[str]:
    model = config.get("model", "-")
    n_tools = len(config.get("tools", []) or [])
    n_mw = len([m for m in (config.get("middleware") or []) if m.get("enabled", True)])
    flavor = config.get("flavor", "agent")
    line2 = f"{n_tools} tools · {n_mw} middleware"
    n_comp = len(config.get("components", []) or [])
    if n_comp:
        line2 += f" · {n_comp} widget{'s' if n_comp != 1 else ''}"
    k = config.get("knowledge") or {}
    kbits = [name for name, key in (("RAG", "rag"), ("Q&A", "qa")) if (k.get(key) or {}).get("enabled")]
    if kbits:
        line2 += " · KB " + "+".join(kbits)
    n_mcp = len(config.get("mcp_servers", []) or [])
    if n_mcp:
        line2 += f" · MCP {n_mcp}"
    if flavor == "deep_agent":
        line2 += f" · subagents {len(config.get('subagents', []) or [])}"
    return [str(model), line2]


_ports = (
    [Port(id="in", io_type="messages", direction="in")],
    [Port(id="out", io_type="messages", direction="out")],
)

register(
    NodeSpec(
        type="agent",
        schema_id="forge/nodes/agent",
        input_ports=_ports[0],
        output_ports=_ports[1],
        factory=agent_factory,
        allows_cycle=True,
        category="agents",
        label="Agent",
        description="ReAct tool loop",
        summarize=_summary,
    )
)

register(
    NodeSpec(
        type="deep_agent",
        schema_id="forge/nodes/agent",
        input_ports=_ports[0],
        output_ports=_ports[1],
        factory=agent_factory,
        allows_cycle=True,
        category="agents",
        label="Deep Agent",
        description="Planning + subagents harness",
        summarize=_summary,
    )
)
