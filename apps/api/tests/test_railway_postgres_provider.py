"""Railway Postgres provider (the 'Railway-only' DB) — postgres template -> DATABASE_URL secret ref,
via a fake GraphQL transport, plus seam wiring (runtime_env exposes DATABASE_URL as a secret ref)."""

from __future__ import annotations

import httpx
from sqlalchemy import select

import ros.services.providers.railway_postgres as pg_mod
from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import ProvisionedBackend
from ros.services import backend_provisioning as bp
from ros.services.providers.railway_postgres import RailwayPostgresProvider


def _fake_railway_pg(monkeypatch, *, database_url: str | None = "postgresql://postgres:pw@host:5432/railway"):
    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        q = _j.loads(req.content)["query"]
        if "projectCreate" in q:
            return httpx.Response(200, json={"data": {"projectCreate": {
                "id": "proj_db", "name": "n",
                "environments": {"edges": [{"node": {"id": "env_db", "name": "production"}}]}}}})
        if "template(" in q:
            return httpx.Response(200, json={"data": {"template": {"id": "tmpl_pg", "serializedConfig": {"x": 1}}}})
        if "templateDeployV2" in q:
            return httpx.Response(200, json={"data": {"templateDeployV2": {"projectId": "proj_db", "workflowId": "wf_db"}}})
        if "variables(" in q:
            return httpx.Response(200, json={"data": {"variables": {"DATABASE_URL": database_url} if database_url else {}}})
        if "projectDelete" in q:
            return httpx.Response(200, json={"data": {"projectDelete": True}})
        return httpx.Response(200, json={"data": {}})

    monkeypatch.setattr(settings, "railway_api_token", "tok")
    monkeypatch.setattr(RailwayPostgresProvider, "_client",
                        lambda self: httpx.AsyncClient(base_url=pg_mod.RAILWAY_API, transport=httpx.MockTransport(handler)))


async def test_provision_creates_postgres_and_returns_url(monkeypatch):
    _fake_railway_pg(monkeypatch)
    outcome = await RailwayPostgresProvider().provision(name="agent-db", spec={})
    assert outcome.external_id == "proj_db"
    assert outcome.secrets["database_url"][0] == "postgresql://postgres:pw@host:5432/railway"
    assert outcome.config["workflow_id"] == "wf_db"


async def test_provision_reports_pending_when_url_not_ready(monkeypatch):
    _fake_railway_pg(monkeypatch, database_url=None)
    outcome = await RailwayPostgresProvider().provision(name="agent-db", spec={})
    assert "database_url" not in outcome.secrets
    assert "pending" in outcome.config["database_url"]


async def test_seam_exposes_database_url_as_runtime_env_secret(monkeypatch):
    _fake_railway_pg(monkeypatch)
    async with SessionLocal() as s:
        handle = await bp.provision_resource(
            s, "t_db", "p_db", agent_id="agentDB", kind="railway-postgres", spec={}
        )
        assert handle["provider"] == "railway-postgres"
        assert handle["secret_refs"]["database_url"].startswith("secret://proj/")
        assert "postgresql://postgres:pw@host:5432/railway" not in str(handle)

        row = (await s.execute(
            select(ProvisionedBackend).where(ProvisionedBackend.project_ref == "proj_db")
        )).scalar_one()
        assert row.provider == "railway-postgres" and row.agent_id == "agentDB"

        env = await bp.runtime_env(s, "t_db", "p_db", agent_id="agentDB")
        assert env["DATABASE_URL"].startswith("secret://proj/")
