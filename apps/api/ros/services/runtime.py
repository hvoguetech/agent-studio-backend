"""Assemble a CompileContext for a run: resolver + materialized project tools.

Tools reference `ctx.auth_resolver` by closure, so we create the context first
(with the resolver) and then materialize tools into it. Unimplemented/broken tool
kinds are skipped so a run still compiles.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from ros.auth_providers.resolver import AuthResolver
from ros.config import settings
from ros.engine.context import CompileContext
from ros.engine.models import default_model_for_credentials
from ros.models import Agent, Project, Tool, Workflow
from ros.secrets.store import SecretStore
from ros.tools.materialize import materialize_tool
from ros.util.ssrf import EgressPolicy

log = logging.getLogger("ros.runtime")


def make_runtime_ctx(tenant_id: str, project_id: str, *, default_model: str | None = None) -> CompileContext:
    return CompileContext(
        tenant_id=tenant_id,
        project_id=project_id,
        auth_resolver=AuthResolver(SecretStore()),
        default_model=default_model or settings.default_model,
    )


def _tool_cfg(t: Tool) -> dict:
    cfg = dict(t.config or {})
    cfg.setdefault("name", t.name)
    cfg.setdefault("kind", t.kind)
    if t.auth_provider_id and not cfg.get("auth_provider_id"):
        cfg["auth_provider_id"] = t.auth_provider_id
    return cfg


async def build_compile_context(
    session, *, tenant_id: str, project_id: str, checkpointer=None, store=None,
    end_user: dict | None = None, run_context: dict | None = None, agent_id: str | None = None,
    workflow_id: str | None = None,
) -> CompileContext:
    # Scope the project load by tenant too (audit H4): callers pass the CALLER's tenant_id, so a
    # cross-tenant project_id resolves to None here (empty config) instead of loading another
    # tenant's project. Belt-and-suspenders behind the router-level ownership check.
    project = (
        await session.execute(
            select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    pconfig = (project.config or {}) if project else {}

    secret_store = SecretStore()

    # Resolve provider API keys (provider -> secret ref) to plaintext for this run only.
    resolved_keys: dict[str, str] = {}
    for provider, ref in (pconfig.get("provider_credentials") or {}).items():
        try:
            val = await secret_store.read_ref(tenant_id=tenant_id, project_id=project_id, ref=ref)
            resolved_keys[provider] = val if isinstance(val, str) else (val.get("key") or val.get("value") or str(val)) if isinstance(val, dict) else str(val)
        except Exception as e:  # noqa: BLE001 - missing/invalid key just falls back to env
            log.warning("Provider key %s unresolved: %s", provider, e)

    # Pick the run's default model. An explicit (non-fake) project default wins; else,
    # if the project has a provider key, default to that provider's model instead of
    # silently degrading agent nodes with no model to the offline `fake:` model.
    explicit = pconfig.get("default_model")
    if explicit and not str(explicit).startswith("fake"):
        default_model = explicit
    else:
        default_model = default_model_for_credentials(resolved_keys) or explicit or settings.default_model

    # Auto-enforce the project's per-run budget as a HARD, pre-action (before_model) cap on EVERY
    # agent node, not just when a builder opts the tenant_budget middleware in per node (governance
    # hard-cap). Prepended to the project default-middleware stack; skipped if one is already set.
    default_mw = list(pconfig.get("default_middleware", []) or [])
    budgets = pconfig.get("budgets") or {}
    cap_cfg: dict = {}
    if budgets.get("max_usd_per_run"):
        cap_cfg["max_usd_per_run"] = budgets["max_usd_per_run"]
    if budgets.get("max_tokens_per_run"):
        cap_cfg["max_tokens_per_run"] = budgets["max_tokens_per_run"]
    if cap_cfg and not any(isinstance(e, dict) and e.get("type") == "tenant_budget" for e in default_mw):
        default_mw = [{"type": "tenant_budget", "config": {**cap_cfg, "on_exceed": "end"}}, *default_mw]

    ctx = CompileContext(
        tenant_id=tenant_id,
        project_id=project_id,
        checkpointer=checkpointer,
        store=store,
        auth_resolver=AuthResolver(secret_store),
        default_model=default_model,
        project_default_mw=default_mw,
    )
    ctx.provider_credentials = resolved_keys
    # Lets nodes (e.g. claude_code) key state on the workflow — its stable per-node workspace is
    # <base>/<workflow_id>/<node_id>. Set here (single choke point) so every run/resume path that
    # goes through build_compile_context gets it, not just the ones that remembered to set it.
    ctx.workflow_id = workflow_id
    ctx.model_aliases = pconfig.get("model_aliases") or {}
    # Project-wide default tools/toolsets granted to every agent node (parallel to default_middleware).
    ctx.project_default_tools = pconfig.get("default_tools") or []
    ctx.project_default_toolsets = pconfig.get("default_toolsets") or []
    ctx.egress_policy = EgressPolicy.from_settings(pconfig.get("egress"))
    ctx.end_user = end_user or None
    # Ephemeral per-run injected context (never persisted, never prompted); consumed by tools
    # for {{ctx.<key>}} templating and by the auth resolver. See CompileContext.run_context.
    ctx.run_context = run_context or {}
    # The run's governed subject (Run.agent_id == ApiKey.id): owner of runtime_env's resources and the
    # principal a mid-run self-provisioning tool gates against. None for operator/console/JWT runs.
    ctx.agent_id = agent_id

    # Per-end-user isolation (2b): a run created by a governed subject (Run.agent_id) gets the
    # RESOLVED env of the resources that subject provisioned — agent-shared UNION this end_user's
    # private set, scoped by (tenant, project, agent, end_user). Operator runs (agent_id None) get
    # nothing. Only the last-write-wins per env var; see resolved_runtime_env for the union rules.
    if agent_id:
        from ros.services.backend_provisioning import resolved_runtime_env

        eu_id = (end_user or {}).get("id")
        ctx.runtime_env = await resolved_runtime_env(
            session, tenant_id, project_id, agent_id=agent_id,
            end_user_id=str(eu_id) if eu_id else None,
        )

    rows = (
        await session.execute(
            select(Tool).where(
                Tool.tenant_id == tenant_id, Tool.project_id == project_id, Tool.enabled.is_(True)
            )
        )
    ).scalars()
    registry: dict[str, object] = {}
    specs: dict[str, dict] = {}
    # LLM name -> human-readable label for streaming/chat activity (keeps the underscore
    # identifier for the model; see CompileContext.tool_display_names). Blank falls back to
    # the identifier so existing tools keep their current label.
    display_names: dict[str, str] = {}
    for t in rows:
        cfg = _tool_cfg(t)
        display_names[t.name] = (cfg.get("display_name") or "").strip() or t.name
        try:
            tool = materialize_tool(cfg, ctx)
            registry[t.id] = tool
            specs[t.id] = {"kind": t.kind, "config": cfg, "tool": tool}
        except Exception as e:  # noqa: BLE001 - skip unimplemented/broken tools
            from ros.util.metrics import incr

            incr("tool.materialize_failed", detail=f"{t.name} ({t.kind}): {e}")
            log.warning("Skipping tool %s (%s): %s", t.name, t.kind, e)
    ctx.tool_registry = registry
    ctx.tool_specs = specs

    # Tool-set membership (set_id -> [tool_id]) so an agent node can reference a whole set via
    # config.toolsets and resolve it to member tools at compile time. One query per run.
    from ros.services.tool_sets import ToolSetService

    ctx.toolset_members = await ToolSetService.members_map(session, tenant_id, project_id)

    # User-defined UI components → widget-tools (Feature 2). Each becomes a tool the agent
    # can call to render a saved html/css template client-side (the tool args are the props).
    from ros.models import Component
    from ros.tools.components import build_component_tool

    try:
        comp_rows = list((
            await session.execute(
                select(Component).where(
                    Component.tenant_id == tenant_id,
                    Component.project_id == project_id,
                    Component.enabled.is_(True),
                )
            )
        ).scalars())
    except Exception as e:  # noqa: BLE001 - a components-table error must not abort the whole run
        log.warning("Skipping components (load failed): %s", e)
        comp_rows = []
    comp_registry: dict[str, object] = {}
    for c in comp_rows:
        # A component is exposed to the model as a widget-tool named after `c.name`; relabel it
        # in the stream with its title (falling back to the name) like any other tool.
        display_names[c.name] = (c.title or "").strip() or c.name
        try:
            comp_registry[c.id] = build_component_tool(
                {
                    "id": c.id, "name": c.name, "description": c.description,
                    "props_schema": c.props_schema, "actions": c.actions, "version": c.version,
                },
                ctx,
            )
        except Exception as e:  # noqa: BLE001 - skip a broken component; don't break the run
            log.warning("Skipping component %s: %s", c.name, e)
    ctx.component_registry = comp_registry
    ctx.tool_display_names = display_names

    # Pre-load enabled MCP servers' tools (one connect per server) so agent nodes can
    # attach them - the agent factory is sync, but MCP discovery is async.
    from ros.models import McpClient
    from ros.tools.mcp import sdk_server_config, server_tools

    mcp_by_client: dict[str, list] = {}
    mcp_configs: dict[str, dict] = {}
    mcp_rows = (
        await session.execute(
            select(McpClient).where(
                McpClient.tenant_id == tenant_id,
                McpClient.project_id == project_id,
                McpClient.enabled.is_(True),
            )
        )
    ).scalars()
    for m in mcp_rows:
        try:
            mcp_by_client[m.id] = await server_tools(m, tenant_id, project_id)
        except Exception as e:  # noqa: BLE001 - an unreachable server must not break the run
            log.warning("MCP server %s unavailable, skipping its tools: %s", m.name, e)
        # Also resolve the SDK-shaped config for the claude_code node's CLI (no connect here; the
        # subprocess connects itself). Independent of server_tools so a bad connection still yields
        # a config a claude_code node could use, and a config-resolve failure doesn't drop the tools.
        try:
            mcp_configs[m.id] = {"name": m.name, "server": await sdk_server_config(m, tenant_id, project_id)}
        except Exception as e:  # noqa: BLE001 - unusable config just means claude_code can't use it
            log.warning("MCP server %s config unavailable for claude_code: %s", m.name, e)
    ctx.mcp_tools_by_client = mcp_by_client
    ctx.mcp_server_configs = mcp_configs

    # Saved agent presets, so an agent node with `agent_ref` mirrors the live preset.
    agent_rows = (
        await session.execute(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.project_id == project_id)
        )
    ).scalars()
    ctx.agent_presets = {a.id: (a.config or {}) for a in agent_rows}

    # Skill library, so a deep_agent node's config["skills"] resolves to real SKILL.md content
    # at compile time. Keyed by id AND name (a workflow may reference either).
    from ros.services.skills import load_skill_library

    ctx.skill_library = await load_skill_library(session, tenant_id, project_id)

    # Workflow executables (keyed by id AND name) so `subworkflow` nodes can reference
    # another workflow as a reusable component.
    wf_rows = (
        await session.execute(
            select(Workflow).where(Workflow.tenant_id == tenant_id, Workflow.project_id == project_id)
        )
    ).scalars()
    wf_map: dict[str, dict] = {}
    for w in wf_rows:
        if w.executable:
            wf_map[w.id] = w.executable
            wf_map.setdefault(w.name, w.executable)
    ctx.workflows = wf_map
    return ctx


def build_compile_context_from_manifest(
    manifest: dict, *, checkpointer=None, store=None,
    end_user: dict | None = None, run_context: dict | None = None,
) -> CompileContext:
    """Rebuild a CompileContext from a RunManifest (services/runtime_manifest.py) instead of the DB —
    the runtime-side twin of build_compile_context for the standalone ros runtime. Materializes the
    same tools/components/agents/workflows from the manifest's serialized configs; provider keys, the
    default model, model aliases, and the tenant_budget hard-cap middleware all arrive precomputed in
    the manifest. Sync (no DB/await).

    Tool auth secrets resolve lazily at call time via an InMemorySecretStore backed by the manifest's
    run-scoped secrets (no master DB / decryption key on the runtime). MCP tools are deferred."""
    from ros.runtime.secret_source import InMemorySecretStore

    ctx = CompileContext(
        tenant_id=manifest["tenant_id"],
        project_id=manifest["project_id"],
        checkpointer=checkpointer,
        store=store,
        auth_resolver=AuthResolver(InMemorySecretStore(manifest.get("secrets") or {})),
        default_model=manifest.get("default_model") or settings.default_model,
        project_default_mw=manifest.get("default_middleware") or [],
    )
    ctx.workflow_id = manifest.get("workflow_id")  # lets nodes (e.g. claude_code) key state on the workflow
    ctx.provider_credentials = manifest.get("provider_credentials") or {}
    ctx.model_aliases = manifest.get("model_aliases") or {}
    # Project-wide default tools/toolsets, carried in the manifest so the VM path grants them too.
    ctx.project_default_tools = manifest.get("default_tools") or []
    ctx.project_default_toolsets = manifest.get("default_toolsets") or []
    ctx.egress_policy = EgressPolicy.from_settings(manifest.get("egress"))
    ctx.end_user = end_user or None
    ctx.run_context = run_context or {}
    # The run's governed subject, carried in the manifest (parallel to the DB path) so a self-
    # provisioning tool gates identically on an isolated VM. Empty for a manifest built without one.
    ctx.agent_id = manifest.get("agent_id")
    # Per-end-user isolation (2b): the agent's resolved provisioned resource env, precomputed by
    # RuntimeManifestService.build (scoped by agent + the run's end_user) so the DB-less runtime
    # gets the same isolation as the DB path. Empty for a manifest built without a governed subject.
    ctx.runtime_env = manifest.get("runtime_env") or {}

    registry: dict[str, object] = {}
    specs: dict[str, dict] = {}
    display_names: dict[str, str] = {}
    for t in manifest.get("tools") or []:
        cfg = dict(t.get("config") or {})
        cfg.setdefault("name", t["name"])
        cfg.setdefault("kind", t["kind"])
        display_names[t["name"]] = (cfg.get("display_name") or "").strip() or t["name"]
        try:
            tool = materialize_tool(cfg, ctx)
            if tool is None:  # e.g. mcp kind — loaded elsewhere
                continue
            registry[t["id"]] = tool
            specs[t["id"]] = {"kind": t["kind"], "config": cfg, "tool": tool}
        except Exception as e:  # noqa: BLE001 - skip a broken tool; don't abort the run
            log.warning("manifest: skipping tool %s (%s): %s", t.get("name"), t.get("kind"), e)
    ctx.tool_registry = registry
    ctx.tool_specs = specs
    ctx.toolset_members = manifest.get("toolset_members") or {}

    from ros.tools.components import build_component_tool

    comp_registry: dict[str, object] = {}
    for c in manifest.get("components") or []:
        display_names[c["name"]] = (c.get("title") or "").strip() or c["name"]
        try:
            comp_registry[c["id"]] = build_component_tool(c, ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("manifest: skipping component %s: %s", c.get("name"), e)
    ctx.component_registry = comp_registry
    ctx.tool_display_names = display_names

    ctx.mcp_tools_by_client = {}  # runtime-side MCP connect is a follow-up
    ctx.mcp_server_configs = {}  # ditto for the claude_code node's SDK mcp_servers on the VM path
    ctx.agent_presets = manifest.get("agent_presets") or {}
    # Skills ride the manifest as plain rows (no DB on the VM), so a deep_agent node mounts the
    # same library there as on master.
    from ros.services.skills import index_skills

    ctx.skill_library = index_skills(manifest.get("skills") or [])
    ctx.workflows = manifest.get("workflows") or {}
    return ctx
