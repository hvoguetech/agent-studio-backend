"""RunManifest — what master serializes for a standalone ros runtime to pull and run.

The manifest is everything the runtime (on a Freestyle VM) needs to rebuild a CompileContext and
compile a workflow WITHOUT the master DB: the workflow executable + the project's tool/agent/
component/mcp/workflow definitions + resolved provider keys. Secret VALUES don't travel except
provider keys (resolved run-scoped, like build_compile_context); tool auth secrets stay refs and are
resolved at call time via a runtime secret source (Part D follow-up).

`build_compile_context_from_manifest` (services/runtime.py) is the consumer. This mirrors the SAME
queries build_compile_context runs — the serialization layer for the control-plane/data-plane split.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from ros.engine.models import default_model_for_credentials
from ros.models import Agent, Component, McpClient, Project, Tool, Workflow
from ros.secrets.store import SecretStore
from ros.services.runtime import _tool_cfg

log = logging.getLogger("ros.runtime_manifest")

MANIFEST_FORMAT = "ros.runtime-manifest/1"

_SECRET_REF_RE = re.compile(r"(?:secret|vault)://[^\s\"']+")


def _collect_secret_refs(obj, out: set[str]) -> None:
    """Every secret://… / vault://… ref appearing anywhere in a JSON-ish structure."""
    if isinstance(obj, str):
        out.update(_SECRET_REF_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_secret_refs(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_secret_refs(v, out)


class RuntimeManifestService:
    @staticmethod
    async def build(
        session, *, tenant_id: str, project_id: str, workflow_id: str,
        agent_id: str | None = None, end_user_id: str | None = None,
    ) -> dict:
        """Assemble the RunManifest for a workflow. Raises LookupError if the project/workflow is
        missing (or belongs to another tenant).

        `agent_id` (the run's governed subject) + `end_user_id` scope the provisioned-resource env
        the manifest carries (2b): with a governed subject, `runtime_env` is the RESOLVED env of the
        agent-shared UNION this end_user's private resources; without one it is empty."""
        project = (await session.execute(
            select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if project is None:
            raise LookupError(f"project {project_id} not found")
        pconfig = project.config or {}

        wf = (await session.execute(
            select(Workflow).where(
                Workflow.id == workflow_id,
                Workflow.tenant_id == tenant_id,
                Workflow.project_id == project_id,
            )
        )).scalar_one_or_none()
        if wf is None:
            raise LookupError(f"workflow {workflow_id} not found")

        # Resolve provider API keys to plaintext for the run (run-scoped), like build_compile_context.
        store = SecretStore()
        provider_credentials: dict[str, str] = {}
        for provider, ref in (pconfig.get("provider_credentials") or {}).items():
            try:
                val = await store.read_ref(tenant_id=tenant_id, project_id=project_id, ref=ref)
                provider_credentials[provider] = val if isinstance(val, str) else str(val)
            except Exception as e:  # noqa: BLE001 - a missing key just falls back to env on the runtime
                log.warning("provider key %s unresolved for manifest: %s", provider, e)

        explicit = pconfig.get("default_model")
        if explicit and not str(explicit).startswith("fake"):
            default_model = explicit
        else:
            from ros.config import settings
            default_model = default_model_for_credentials(provider_credentials) or explicit or settings.default_model

        tool_rows = (await session.execute(
            select(Tool).where(
                Tool.tenant_id == tenant_id, Tool.project_id == project_id, Tool.enabled.is_(True)
            )
        )).scalars()
        tools = [{"id": t.id, "name": t.name, "kind": t.kind, "config": _tool_cfg(t)} for t in tool_rows]

        from ros.services.tool_sets import ToolSetService
        toolset_members = await ToolSetService.members_map(session, tenant_id, project_id)

        comp_rows = (await session.execute(
            select(Component).where(
                Component.tenant_id == tenant_id, Component.project_id == project_id,
                Component.enabled.is_(True),
            )
        )).scalars()
        components = [{
            "id": c.id, "name": c.name, "description": c.description, "title": c.title,
            "props_schema": c.props_schema, "actions": c.actions, "version": c.version,
        } for c in comp_rows]

        mcp_rows = (await session.execute(
            select(McpClient).where(
                McpClient.tenant_id == tenant_id, McpClient.project_id == project_id,
                McpClient.enabled.is_(True),
            )
        )).scalars()
        # Only identity for now — the runtime connects to MCP servers itself (deferred); carrying
        # full MCP config + its secrets is part of the runtime-secret-source follow-up.
        mcp_clients = [{"id": m.id, "name": m.name} for m in mcp_rows]

        agent_rows = (await session.execute(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.project_id == project_id)
        )).scalars()
        agent_presets = {a.id: (a.config or {}) for a in agent_rows}

        wf_rows = (await session.execute(
            select(Workflow).where(Workflow.tenant_id == tenant_id, Workflow.project_id == project_id)
        )).scalars()
        workflows: dict[str, dict] = {}
        for w in wf_rows:
            if w.executable:
                workflows[w.id] = w.executable
                workflows.setdefault(w.name, w.executable)

        # Resolve the secret refs the definitions reference, run-scoped, so tool auth resolves on the
        # runtime WITHOUT the master DB/decryption key. (Auth-provider-mediated secrets — tool ->
        # auth_provider_id -> credentials_ref — are a follow-up: the manifest would also carry the
        # auth-provider configs.) Direct config refs are handled here.
        refs: set[str] = set()
        for t in tools:
            _collect_secret_refs(t["config"], refs)
        for preset in agent_presets.values():
            _collect_secret_refs(preset, refs)
        for c in components:
            _collect_secret_refs(c, refs)
        secrets: dict[str, object] = {}
        for ref in refs:
            try:
                secrets[ref] = await store.read_ref(tenant_id=tenant_id, project_id=project_id, ref=ref)
            except Exception as e:  # noqa: BLE001 - an unresolved ref is omitted; the tool handles the miss
                log.warning("manifest secret %s unresolved: %s", ref, e)

        # Mirror build_compile_context's hard-cap prepend: the project's per-run budget becomes a
        # tenant_budget before_model middleware on every agent node, so the runtime enforces the
        # SAME governance hard-cap the DB path does.
        default_mw = list(pconfig.get("default_middleware", []) or [])
        budgets = pconfig.get("budgets") or {}
        cap_cfg: dict = {}
        if budgets.get("max_usd_per_run"):
            cap_cfg["max_usd_per_run"] = budgets["max_usd_per_run"]
        if budgets.get("max_tokens_per_run"):
            cap_cfg["max_tokens_per_run"] = budgets["max_tokens_per_run"]
        if cap_cfg and not any(isinstance(e, dict) and e.get("type") == "tenant_budget" for e in default_mw):
            default_mw = [{"type": "tenant_budget", "config": {**cap_cfg, "on_exceed": "end"}}, *default_mw]

        # Per-end-user isolation (2b): the agent's RESOLVED provisioned-resource env, scoped by the
        # run's governed subject + end_user. Empty when the manifest is built without a governed
        # subject (operator run). The runtime exports these to os.environ before driving so the
        # agent reaches its own DB/queue/etc.
        runtime_env: dict[str, str] = {}
        if agent_id:
            from ros.services.backend_provisioning import resolved_runtime_env

            runtime_env = await resolved_runtime_env(
                session, tenant_id, project_id, agent_id=agent_id, end_user_id=end_user_id,
            )

        return {
            "format": MANIFEST_FORMAT,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "workflow_id": workflow_id,
            "agent_id": agent_id,  # the run's governed subject (gates a mid-run self-provision on the VM)
            "executable": wf.executable or {},
            "default_model": default_model,
            "default_middleware": default_mw,
            "default_tools": pconfig.get("default_tools") or [],
            "default_toolsets": pconfig.get("default_toolsets") or [],
            "model_aliases": pconfig.get("model_aliases") or {},
            "egress": pconfig.get("egress"),
            "provider_credentials": provider_credentials,
            "tools": tools,
            "toolset_members": toolset_members,
            "components": components,
            "mcp_clients": mcp_clients,
            "agent_presets": agent_presets,
            "workflows": workflows,
            "secrets": secrets,  # run-scoped resolved refs for tool call-time auth on the runtime
            "runtime_env": runtime_env,  # resolved provisioned-resource env (2b), scoped by agent+end_user
        }
