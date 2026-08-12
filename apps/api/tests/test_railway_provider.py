"""Railway provider — request-building + outcome via a fake GraphQL transport, and seam wiring.

The live Railway GraphQL shapes are flagged live-verify in the provider; these tests validate the
provider's own logic (project -> service -> variables -> domain, teardown) and that it plugs into
the ResourceProvider seam / orchestrator (row + runtime_env) with no secrets to store."""

from __future__ import annotations

import httpx
from sqlalchemy import select

import ros.services.providers.railway as railway_mod
from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import ProvisionedBackend
from ros.services import backend_provisioning as bp
from ros.services.providers.railway import RailwayProvider


def _fake_railway(monkeypatch, *, calls: list | None = None, fail_domain: bool = False):
    """Point RailwayProvider._client at a MockTransport that answers each GraphQL op by name."""

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        op = _j.loads(req.content)
        q = op["query"]
        if calls is not None:
            calls.append(q.strip().split("{", 1)[0].strip())
        if "projectCreate" in q:
            return httpx.Response(200, json={"data": {"projectCreate": {
                "id": "proj_1", "name": "n",
                "environments": {"edges": [{"node": {"id": "env_prod", "name": "production"}}]},
            }}})
        if "serviceCreate" in q:
            return httpx.Response(200, json={"data": {"serviceCreate": {"id": "svc_1", "name": "n"}}})
        if "variableUpsert" in q:
            return httpx.Response(200, json={"data": {"variableUpsert": True}})
        if "serviceDomainCreate" in q:
            if fail_domain:
                return httpx.Response(200, json={"errors": [{"message": "no http port"}]})
            return httpx.Response(200, json={"data": {"serviceDomainCreate": {"domain": "svc-1.up.railway.app"}}})
        if "projectDelete" in q:
            return httpx.Response(200, json={"data": {"projectDelete": True}})
        return httpx.Response(200, json={"data": {}})

    monkeypatch.setattr(
        RailwayProvider, "_client",
        lambda self: httpx.AsyncClient(base_url=railway_mod.RAILWAY_API, transport=httpx.MockTransport(handler)),
    )


async def test_provision_creates_project_service_and_domain(monkeypatch):
    monkeypatch.setattr(settings, "railway_api_token", "tok")
    calls: list = []
    _fake_railway(monkeypatch, calls=calls)
    outcome = await RailwayProvider().provision(name="agent-x", spec={"image": "nginx:latest", "variables": {"FOO": "bar"}})
    assert outcome.external_id == "proj_1"
    assert outcome.endpoint_url == "https://svc-1.up.railway.app"
    assert outcome.public["service_id"] == "svc_1"
    assert outcome.config["environment_id"] == "env_prod"
    assert len(calls) == 4  # project + service + variable + domain


async def test_provision_domain_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(settings, "railway_api_token", "tok")
    _fake_railway(monkeypatch, fail_domain=True)
    outcome = await RailwayProvider().provision(name="agent-x", spec={"image": "nginx:latest"})
    assert outcome.external_id == "proj_1"
    assert outcome.endpoint_url is None  # no domain, but the project/service still provisioned


async def test_is_enabled_follows_token(monkeypatch):
    monkeypatch.setattr(settings, "railway_api_token", "")
    assert RailwayProvider().is_enabled() is False
    monkeypatch.setattr(settings, "railway_api_token", "tok")
    assert RailwayProvider().is_enabled() is True


async def test_seam_provisions_railway_row_and_runtime_env(monkeypatch):
    monkeypatch.setattr(settings, "railway_api_token", "tok")
    _fake_railway(monkeypatch)
    async with SessionLocal() as s:
        handle = await bp.provision_resource(
            s, "t_rw", "p_rw", agent_id="agentR", kind="railway", spec={"image": "nginx:latest"}
        )
        assert handle["provider"] == "railway"
        assert handle["endpoint_url"] == "https://svc-1.up.railway.app"
        assert handle["secret_refs"] == {}  # railway compute exposes no stored secrets here

        row = (await s.execute(
            select(ProvisionedBackend).where(ProvisionedBackend.project_ref == "proj_1")
        )).scalar_one()
        assert row.provider == "railway" and row.agent_id == "agentR"

        env = await bp.runtime_env(s, "t_rw", "p_rw", agent_id="agentR")
        assert env["SERVICE_URL"] == "https://svc-1.up.railway.app"
