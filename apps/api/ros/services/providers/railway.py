"""Railway resource provider — an isolated Railway project (compute + apps) per agent.

Implements the ResourceProvider seam over Railway's public GraphQL API
(https://backboard.railway.com/graphql/v2, `Authorization: Bearer <token>`). Provisions a project,
a service (from a Docker image or GitHub repo), service variables, and a public domain; returns the
domain as the runtime SERVICE_URL. Teardown deletes the project (which removes its services).

⚠️ LIVE-VERIFY: Railway does not publish its GraphQL schema in the LLM docs. `serviceCreate`
(including `source: {image}`) is confirmed from the Railway skill reference; `projectCreate`,
`variableUpsert`, `serviceDomainCreate`, and `projectDelete` use Railway's documented public-API
shapes but must be verified against a live token on first use — they're isolated to this module.
Tests exercise request-building + outcome logic via a fake transport, not the live API.
"""

from __future__ import annotations

import logging

import httpx

from ros.config import settings
from ros.services.providers.base import ProvisionError, ProvisionOutcome

logger = logging.getLogger(__name__)

RAILWAY_API = "https://backboard.railway.com"
_TIMEOUT = 30.0

_PROJECT_CREATE = """
mutation($input: ProjectCreateInput!) {
  projectCreate(input: $input) { id name environments { edges { node { id name } } } }
}"""
_SERVICE_CREATE = """
mutation($input: ServiceCreateInput!) { serviceCreate(input: $input) { id name } }"""
_VARIABLE_UPSERT = """
mutation($input: VariableUpsertInput!) { variableUpsert(input: $input) }"""
_DOMAIN_CREATE = """
mutation($input: ServiceDomainCreateInput!) { serviceDomainCreate(input: $input) { domain } }"""
_PROJECT_DELETE = """
mutation($id: String!) { projectDelete(id: $id) }"""


def _prune(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


async def _gql(token: str, query: str, variables: dict, *, client: httpx.AsyncClient) -> dict:
    """POST a GraphQL op and return its `data`, raising ProvisionError on transport / GraphQL error."""
    try:
        resp = await client.post(
            "/graphql/v2",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
        )
    except httpx.HTTPError as e:
        raise ProvisionError(f"railway request failed: {e}") from e
    if resp.status_code != 200:
        raise ProvisionError(f"railway graphql -> {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if body.get("errors"):
        raise ProvisionError(f"railway graphql error: {body['errors']}")
    return body.get("data") or {}


class RailwayProvider:
    kind = "railway"

    def is_enabled(self) -> bool:
        return bool(settings.railway_api_token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=RAILWAY_API, timeout=_TIMEOUT)

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        token = settings.railway_api_token
        source: dict = {}
        if spec.get("image"):
            source = {"image": spec["image"]}
        elif spec.get("repo"):
            source = _prune({"repo": spec["repo"], "branch": spec.get("branch")})
        variables = spec.get("variables") or {}

        async with self._client() as client:
            proj = (await _gql(token, _PROJECT_CREATE,
                               {"input": _prune({"name": name, "workspaceId": settings.railway_workspace_id})},
                               client=client)).get("projectCreate") or {}
            project_id = proj.get("id")
            if not project_id:
                raise ProvisionError(f"railway projectCreate returned no id: {proj!r}")
            envs = [e["node"] for e in (proj.get("environments") or {}).get("edges", [])]
            env_id = next((e["id"] for e in envs if (e.get("name") or "").lower() == "production"),
                          envs[0]["id"] if envs else None)

            service_id = None
            domain = None
            try:
                svc_input = _prune({"projectId": project_id, "name": name,
                                    "source": source or None, "environmentId": env_id})
                service = (await _gql(token, _SERVICE_CREATE, {"input": svc_input}, client=client)).get("serviceCreate") or {}
                service_id = service.get("id")

                for k, v in variables.items():
                    await _gql(token, _VARIABLE_UPSERT, {"input": _prune({
                        "projectId": project_id, "environmentId": env_id,
                        "serviceId": service_id, "name": k, "value": str(v),
                    })}, client=client)

                # Public domain — best-effort (a service with no HTTP port has no domain yet).
                try:
                    dom = (await _gql(token, _DOMAIN_CREATE, {"input": _prune({
                        "environmentId": env_id, "serviceId": service_id,
                    })}, client=client)).get("serviceDomainCreate") or {}
                    domain = dom.get("domain")
                except ProvisionError as e:
                    logger.warning("railway domain create failed for %s: %s", project_id, e)
            except Exception as e:
                await self._safe_delete(token, project_id)
                raise ProvisionError(f"railway provisioning failed after project create ({project_id}): {e}") from e

        endpoint = f"https://{domain}" if domain else None
        return ProvisionOutcome(
            external_id=project_id, endpoint_url=endpoint, secrets={},
            public=_prune({"service_id": service_id, "domain": domain}),
            config=_prune({"environment_id": env_id, "service_id": service_id}),
        )

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        await self._safe_delete(settings.railway_api_token, external_id)

    async def _safe_delete(self, token: str, project_id: str) -> None:
        try:
            async with self._client() as client:
                await _gql(token, _PROJECT_DELETE, {"id": project_id}, client=client)
        except Exception:  # noqa: BLE001 - best-effort; a leaked project is logged for manual cleanup
            logger.warning("best-effort delete of Railway project %s failed", project_id, exc_info=True)
