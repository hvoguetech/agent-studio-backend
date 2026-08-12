"""Railway Postgres provider — a dedicated managed Postgres per agent (the 'Railway-only' DB).

Mirrors the Queue provider: provisions an isolated Railway project with a managed Postgres (via
Railway's `postgres` template) and returns its connection URL as `DATABASE_URL` (a secret ref). Use
this instead of the Supabase provider when the database should live on Railway alongside the agent's
compute + queue (one vendor). Reuses the Railway GraphQL client. Teardown deletes the project.

⚠️ LIVE-VERIFY: `template` → `templateDeployV2` → the DATABASE_URL read-back is the least-verified
path (async deploy workflow; the connection var lands on the DB service afterward). Confirm against
a live Railway token.
"""

from __future__ import annotations

import logging

import httpx

from ros.config import settings
from ros.services.providers.base import ProvisionError, ProvisionOutcome
from ros.services.providers.railway import (
    RAILWAY_API,
    _PROJECT_CREATE,
    _PROJECT_DELETE,
    _TIMEOUT,
    _gql,
    _prune,
)

logger = logging.getLogger(__name__)

_TEMPLATE = """
query($code: String!) { template(code: $code) { id serializedConfig } }"""
_TEMPLATE_DEPLOY = """
mutation($input: TemplateDeployV2Input!) { templateDeployV2(input: $input) { projectId workflowId } }"""
_PROJECT_VARS = """
query($projectId: String!, $environmentId: String!) { variables(projectId: $projectId, environmentId: $environmentId) }"""


class RailwayPostgresProvider:
    kind = "railway-postgres"

    def is_enabled(self) -> bool:
        return bool(settings.railway_api_token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=RAILWAY_API, timeout=_TIMEOUT)

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        token = settings.railway_api_token
        async with self._client() as client:
            proj = (await _gql(token, _PROJECT_CREATE,
                               {"input": _prune({"name": f"{name}-db", "workspaceId": settings.railway_workspace_id})},
                               client=client)).get("projectCreate") or {}
            project_id = proj.get("id")
            if not project_id:
                raise ProvisionError(f"railway projectCreate returned no id: {proj!r}")
            envs = [e["node"] for e in (proj.get("environments") or {}).get("edges", [])]
            env_id = next((e["id"] for e in envs if (e.get("name") or "").lower() == "production"),
                          envs[0]["id"] if envs else None)
            workspace_id = settings.railway_workspace_id or proj.get("workspaceId")

            try:
                tmpl = (await _gql(token, _TEMPLATE, {"code": "postgres"}, client=client)).get("template") or {}
                template_id = tmpl.get("id")
                if not template_id:
                    raise ProvisionError("postgres template not found")
                deploy = (await _gql(token, _TEMPLATE_DEPLOY, {"input": _prune({
                    "templateId": template_id, "serializedConfig": tmpl.get("serializedConfig"),
                    "projectId": project_id, "environmentId": env_id, "workspaceId": workspace_id,
                })}, client=client)).get("templateDeployV2") or {}
                workflow_id = deploy.get("workflowId")
                db_url = await self._read_database_url(token, project_id, env_id, client=client)
            except Exception as e:
                await self._safe_delete(token, project_id)
                raise ProvisionError(f"railway-postgres provisioning failed after project create ({project_id}): {e}") from e

        secrets: dict[str, tuple[str, str]] = {}
        if db_url:
            secrets["database_url"] = (db_url, "db_url")
        config = _prune({"environment_id": env_id, "workflow_id": workflow_id})
        if not db_url:
            config["database_url"] = "pending (read from Railway once the deploy workflow completes)"
        return ProvisionOutcome(external_id=project_id, endpoint_url=None, secrets=secrets, public={}, config=config)

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        await self._safe_delete(settings.railway_api_token, external_id)

    async def _safe_delete(self, token: str, project_id: str) -> None:
        try:
            async with self._client() as client:
                await _gql(token, _PROJECT_DELETE, {"id": project_id}, client=client)
        except Exception:  # noqa: BLE001 - best-effort; a leaked project is logged for manual cleanup
            logger.warning("best-effort delete of Railway postgres project %s failed", project_id, exc_info=True)

    async def _read_database_url(self, token: str, project_id: str, env_id: str, *, client: httpx.AsyncClient) -> str | None:
        """Best-effort read of the Postgres connection URL from the project's env variables.
        LIVE-VERIFY: variable shape / key name may differ; a miss returns None (reported pending)."""
        try:
            data = await _gql(token, _PROJECT_VARS, {"projectId": project_id, "environmentId": env_id}, client=client)
        except ProvisionError:
            return None
        vars_map = data.get("variables")
        if not isinstance(vars_map, dict):
            return None
        for key in ("DATABASE_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL"):
            if isinstance(vars_map.get(key), str) and vars_map[key]:
                return vars_map[key]
        for v in vars_map.values():
            if isinstance(v, str) and v.startswith(("postgres://", "postgresql://")):
                return v
        return None
