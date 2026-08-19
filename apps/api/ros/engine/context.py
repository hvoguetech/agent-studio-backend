"""CompileContext - the per-compile dependency bundle (Doc 2 §6).

Carries everything `NodeSpec.factory` / `MW_BUILDERS` need: tenant scoping, the
checkpointer + store, the tracer callback, the materialized tool registry, the auth
resolver, the sandbox, and model-provider credential bindings. Kept dependency-light
(plain dataclass with optionals) so the engine core is unit-testable in isolation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompileContext:
    tenant_id: str
    project_id: str

    # LangGraph durability + long-term memory.
    checkpointer: Any = None
    store: Any = None

    # Tracing callback handler attached to every astream/ainvoke.
    tracer: Any = None

    # Materialized tools: tool_id -> StructuredTool (built by tools.materialize).
    tool_registry: dict[str, Any] = field(default_factory=dict)
    # tool_id -> {"kind", "config", "tool"} so the tool_call node can invoke directly.
    tool_specs: dict[str, dict] = field(default_factory=dict)
    # LLM tool name (the underscore identifier the model calls) -> human-readable label
    # shown in streaming/chat activity. Populated for user tools (config.display_name) and
    # UI components (their title), each falling back to the identifier when unset. The model
    # never sees this - it only relabels tool_calls in the stream for end-user surfaces.
    tool_display_names: dict[str, str] = field(default_factory=dict)
    # MCP server id -> list of native LangChain tools (the server's enabled tools),
    # pre-loaded by the runtime assembler so the sync agent factory can attach them.
    mcp_tools_by_client: dict[str, list] = field(default_factory=dict)
    # Materialized UI components (component_id -> widget StructuredTool); attached to an
    # agent via config["components"], the same way tools are (Feature 2 - generative UI).
    component_registry: dict[str, Any] = field(default_factory=dict)

    # Tool-set membership (tool_set_id -> [tool_id, ...]) for the project, populated by the
    # runtime assembler. Lets an agent be granted a whole set via config.toolsets and have it
    # resolve to the set's member tools at compile time (see resolve_tool_ids).
    toolset_members: dict[str, list[str]] = field(default_factory=dict)

    # Cross-cutting services.
    auth_resolver: Any = None
    sandbox: Any = None

    # SSRF egress policy (project override of the global allow/deny + private-range
    # block), applied to every outbound HTTP call a workflow makes (tools, webhooks,
    # web_fetch). Set by the runtime assembler from project.config.egress.
    egress_policy: Any = None

    # Model config.
    default_model: str | None = None
    provider_credentials: dict[str, str] = field(default_factory=dict)
    # Project model aliases: logical name -> concrete model ref (e.g. {"fast": "openai:gpt-4.1-nano",
    # "smart": "anthropic:claude-sonnet-4-6"}). A node's `model` may reference an alias; resolve_model
    # expands it (one level) so models can be swapped centrally without editing every workflow.
    model_aliases: dict[str, str] = field(default_factory=dict)

    # The end user this run acts for (identity, Feature 3). Generic app-defined shape
    # ({id, roles?, attributes?, entitlements?, …}); surfaced to agent prompts (awareness)
    # and tool templating ({{ctx.end_user…}} / on-behalf-of calls). None = anonymous.
    end_user: dict | None = None

    # Ephemeral per-run request context (Feature: per-run context injection). Values a
    # server-side caller passes on the run's EXECUTION request (stream/resume, via the
    # `X-ROS-Context` header) for tools to inject into outbound calls as {{ctx.<key>}} -
    # e.g. a per-user session cookie / CSRF token when acting on the caller's behalf. UNLIKE
    # end_user this is NEVER persisted (not on the thread/run/checkpointer/trace) and NEVER
    # placed in the LLM prompt or an LLM-visible tool arg; it reaches only the tool's outbound
    # HTTP request and the auth resolver. Put per-request secrets HERE, not in end_user (which
    # is embedded in the prompt and stored on the thread).
    run_context: dict = field(default_factory=dict)

    # The run's GOVERNED SUBJECT: the ApiKey id this run acts as (Run.agent_id == ApiKey.id) — the
    # owner of the resources surfaced in `runtime_env`, and the principal a mid-run self-provisioning
    # tool gates against (its `backend:provision` capability allow-list + per-subject capacity cap).
    # None for operator / console / JWT / service runs (no governed subject); those cannot
    # self-provision from inside a run and must use the HTTP provisioning route instead.
    agent_id: str | None = None

    # The agent's provisioned, per-(agent, end_user) resource environment (per-end-user isolation
    # 2b): standard env var name -> RESOLVED value (e.g. DATABASE_URL, REDIS_URL, endpoint URLs) for
    # the durable resources this run's governed subject (Run.agent_id) provisioned — the agent-shared
    # set UNION this end_user's private set. Empty for runs with no governed subject (operator /
    # console / JWT / service). This is the leak-safe channel: it is per-run state on the context, so
    # concurrent runs on shared master never see each other's creds. On an isolated single-run VM the
    # runtime entrypoint additionally exports these to os.environ so the agent's own code/tools reach
    # its resources (ros.runtime.env.apply_runtime_env); that export never runs on master.
    runtime_env: dict[str, str] = field(default_factory=dict)

    # Project-level default middleware, prepended to every agent stack (Doc 2 §8).
    project_default_mw: list[dict] = field(default_factory=list)

    # Project-level default tools/toolsets (project.config.default_tools / default_toolsets), granted
    # to EVERY agent node on top of the tools it lists itself. The node merges these into its own tool
    # ids; resolve_tool_ids de-dups by id, so a node that also lists a default doesn't bind it twice.
    # The tool analogue of project_default_mw — one capability set every agent in the project gets.
    project_default_tools: list[str] = field(default_factory=list)
    project_default_toolsets: list[str] = field(default_factory=list)

    # Saved agent presets (agent_id -> config), so an agent node can mirror one by
    # `agent_ref` and pick up edits made in the Agents tab without re-saving the workflow.
    agent_presets: dict[str, dict] = field(default_factory=dict)

    # The project's skill library (skill_id -> {name, description, content, files}), populated by
    # the runtime assembler. A deep_agent node names skills in config["skills"]; the factory
    # materializes just those into a read-only /skills/ mount (ros.skills). Keyed by id AND name
    # so a workflow can reference either.
    skill_library: dict[str, dict] = field(default_factory=dict)

    # Project workflows' executables (id -> definition) so a `subworkflow` node can
    # compile a referenced workflow as a nested graph. `compiling` tracks in-progress
    # ids to break recursion cycles.
    workflows: dict[str, dict] = field(default_factory=dict)
    compiling: set = field(default_factory=set)

    def tools_for(self, ids: Sequence[str]) -> list[Any]:
        """Resolve tool ids to materialized tools, skipping unknown ids.

        Unknown ids are tolerated at compile time and surfaced by the validator
        instead, so a partially-wired draft still compiles for preview.
        """
        out = []
        for i in ids or []:
            tool = self.tool_registry.get(i)
            if tool is not None:
                out.append(tool)
        return out

    def resolve_tool_ids(self, tool_ids: Sequence[str] | None, set_ids: Sequence[str] | None = None) -> list[str]:
        """Combine explicit tool ids with the members of any referenced tool sets into one
        order-stable, de-duplicated id list. Unknown set ids contribute nothing (tolerant,
        like tools_for), so an agent can be granted individual tools AND whole sets at once."""
        seen: set[str] = set()
        out: list[str] = []
        for tid in tool_ids or []:
            if tid not in seen:
                seen.add(tid)
                out.append(tid)
        for sid in set_ids or []:
            for tid in self.toolset_members.get(sid, []):
                if tid not in seen:
                    seen.add(tid)
                    out.append(tid)
        return out

    def components_for(self, ids: Sequence[str]) -> list[Any]:
        """Resolve component ids to materialized widget-tools, skipping unknown ids
        (a deleted component just drops out, like tools_for)."""
        out = []
        for i in ids or []:
            tool = self.component_registry.get(i)
            if tool is not None:
                out.append(tool)
        return out

    def has_entitlements(self, required) -> bool:
        """True if the run's end_user holds ALL of `required` (matched against roles ∪
        entitlements). Empty/absent requirement → allowed, anonymous user → denied. The
        server-side gate for tools that declare `required_entitlements` (Feature 3b)."""
        req = [r for r in (required or []) if r]
        if not req:
            return True
        eu = self.end_user or {}
        have = set(eu.get("entitlements") or []) | set(eu.get("roles") or [])
        return all(r in have for r in req)

    def sandbox_backend_for(self, config: dict) -> Any:
        """Deep-agent sandbox backend from a node's sandbox config. (Phase 3+.)"""
        return self.sandbox
