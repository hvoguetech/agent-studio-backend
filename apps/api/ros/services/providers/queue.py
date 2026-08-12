"""Queue resource provider — BullMQ on Railway: a dedicated Redis per agent + a queue namespace.

Provisions an isolated Railway project with a managed Redis (via Railway's `redis` template) and
returns its connection URL as `REDIS_URL` (a secret ref) plus the BullMQ queue name. BullMQ queues
are logical namespaces on that Redis; the workers run in the agent's compute (a Freestyle VM or a
Railway service) which gets `REDIS_URL` injected via runtime_env. This is the DURABLE queue used for
HITL waits, external signals with timeouts, and work that must survive the run.

Reuses the Railway GraphQL client. Teardown deletes the project.

⚠️ LIVE-VERIFY: the Redis provisioning path (`template` -> `templateDeployV2`) and especially the
REDIS_URL read-back are the least-verified shapes — the deploy runs an async workflow and the
connection var lands on the Redis service afterward. Isolated here; confirm against a live token.
"""

from __future__ import annotations

import logging

import httpx

from ros.config import settings
from ros.services.providers.base import ProvisionError, ProvisionOutcome
from ros.services.providers.railway import (
    _PROJECT_CREATE,
    _PROJECT_DELETE,
    _TIMEOUT,
    RAILWAY_API,
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


class QueueProvider:
    kind = "queue"

    def is_enabled(self) -> bool:
        return bool(settings.railway_api_token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=RAILWAY_API, timeout=_TIMEOUT)

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        token = settings.railway_api_token
        queue_name = spec.get("queue_name") or name

        async with self._client() as client:
            proj = (await _gql(token, _PROJECT_CREATE,
                               {"input": _prune({"name": f"{name}-queue", "workspaceId": settings.railway_workspace_id})},
                               client=client)).get("projectCreate") or {}
            project_id = proj.get("id")
            if not project_id:
                raise ProvisionError(f"railway projectCreate returned no id: {proj!r}")
            envs = [e["node"] for e in (proj.get("environments") or {}).get("edges", [])]
            env_id = next((e["id"] for e in envs if (e.get("name") or "").lower() == "production"),
                          envs[0]["id"] if envs else None)
            workspace_id = settings.railway_workspace_id or proj.get("workspaceId")

            try:
                tmpl = (await _gql(token, _TEMPLATE, {"code": "redis"}, client=client)).get("template") or {}
                template_id = tmpl.get("id")
                if not template_id:
                    raise ProvisionError("redis template not found")
                deploy = (await _gql(token, _TEMPLATE_DEPLOY, {"input": _prune({
                    "templateId": template_id, "serializedConfig": tmpl.get("serializedConfig"),
                    "projectId": project_id, "environmentId": env_id, "workspaceId": workspace_id,
                })}, client=client)).get("templateDeployV2") or {}
                workflow_id = deploy.get("workflowId")
                redis_url = await self._read_redis_url(token, project_id, env_id, client=client)
            except Exception as e:
                await self._safe_delete(token, project_id)
                raise ProvisionError(f"queue provisioning failed after project create ({project_id}): {e}") from e

        secrets: dict[str, tuple[str, str]] = {}
        if redis_url:
            secrets["redis_url"] = (redis_url, "url")
        config = _prune({"environment_id": env_id, "workflow_id": workflow_id, "queue_name": queue_name})
        if not redis_url:
            # The deploy workflow is async; the connection var lands afterward. Surface that rather
            # than pretend REDIS_URL is ready.
            config["redis_url"] = "pending (read from Railway once the deploy workflow completes)"
        return ProvisionOutcome(
            external_id=project_id, endpoint_url=None, secrets=secrets,
            public={"queue_name": queue_name}, config=config,
        )

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        await self._safe_delete(settings.railway_api_token, external_id)

    async def _safe_delete(self, token: str, project_id: str) -> None:
        try:
            async with self._client() as client:
                await _gql(token, _PROJECT_DELETE, {"id": project_id}, client=client)
        except Exception:  # noqa: BLE001 - best-effort; a leaked project is logged for manual cleanup
            logger.warning("best-effort delete of Railway queue project %s failed", project_id, exc_info=True)

    async def _read_redis_url(self, token: str, project_id: str, env_id: str, *, client: httpx.AsyncClient) -> str | None:
        """Best-effort read of the Redis connection URL from the project's env variables. LIVE-VERIFY:
        the variables shape / key name may differ; a miss returns None (URL reported as pending)."""
        try:
            data = await _gql(token, _PROJECT_VARS, {"projectId": project_id, "environmentId": env_id}, client=client)
        except ProvisionError:
            return None
        vars_map = data.get("variables")
        if not isinstance(vars_map, dict):
            return None
        for key in ("REDIS_URL", "REDIS_PRIVATE_URL", "REDIS_PUBLIC_URL"):
            if isinstance(vars_map.get(key), str) and vars_map[key]:
                return vars_map[key]
        for v in vars_map.values():
            if isinstance(v, str) and v.startswith("redis://"):
                return v
        return None
