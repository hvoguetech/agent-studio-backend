"""Queue provider (BullMQ on Railway) — dedicated Redis + REDIS_URL secret, via a fake transport.

Validates the provider's flow (projectCreate -> redis template -> templateDeployV2 -> read
REDIS_URL) and that it plugs into the seam so an agent's REDIS_URL is exposed via runtime_env as a
secret ref. The live Railway shapes are flagged live-verify in the provider."""

from __future__ import annotations

import httpx
from sqlalchemy import select

import ros.services.providers.queue as queue_mod
from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import ProvisionedBackend
from ros.services import backend_provisioning as bp
from ros.services.providers.queue import QueueProvider


def _fake_railway_queue(monkeypatch, *, redis_url: str | None = "redis://default:pw@host:6379"):
    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        q = _j.loads(req.content)["query"]
        if "projectCreate" in q:
            return httpx.Response(200, json={"data": {"projectCreate": {
                "id": "proj_q", "name": "n",
                "environments": {"edges": [{"node": {"id": "env_q", "name": "production"}}]}}}})
        if "template(code" in q or "template(" in q:
            return httpx.Response(200, json={"data": {"template": {"id": "tmpl_redis", "serializedConfig": {"x": 1}}}})
        if "templateDeployV2" in q:
            return httpx.Response(200, json={"data": {"templateDeployV2": {"projectId": "proj_q", "workflowId": "wf_1"}}})
        if "variables(" in q:
            vars_map = {"REDIS_URL": redis_url} if redis_url else {}
            return httpx.Response(200, json={"data": {"variables": vars_map}})
        if "projectDelete" in q:
            return httpx.Response(200, json={"data": {"projectDelete": True}})
        return httpx.Response(200, json={"data": {}})

    monkeypatch.setattr(settings, "railway_api_token", "tok")
    monkeypatch.setattr(QueueProvider, "_client",
                        lambda self: httpx.AsyncClient(base_url=queue_mod.RAILWAY_API, transport=httpx.MockTransport(handler)))


async def test_provision_creates_redis_and_returns_url(monkeypatch):
    _fake_railway_queue(monkeypatch)
    outcome = await QueueProvider().provision(name="agent-q", spec={"queue_name": "jobs"})
    assert outcome.external_id == "proj_q"
    assert outcome.secrets["redis_url"][0] == "redis://default:pw@host:6379"
    assert outcome.public["queue_name"] == "jobs"
    assert outcome.config["workflow_id"] == "wf_1"


async def test_provision_reports_pending_when_url_not_ready(monkeypatch):
    _fake_railway_queue(monkeypatch, redis_url=None)
    outcome = await QueueProvider().provision(name="agent-q", spec={})
    assert "redis_url" not in outcome.secrets
    assert "pending" in outcome.config["redis_url"]


async def test_seam_exposes_redis_url_as_runtime_env_secret(monkeypatch):
    _fake_railway_queue(monkeypatch)
    async with SessionLocal() as s:
        handle = await bp.provision_resource(
            s, "t_q", "p_q", agent_id="agentQ", kind="queue", spec={"queue_name": "jobs"}
        )
        assert handle["provider"] == "queue"
        # REDIS_URL is stored as a secret ref, not returned in plaintext.
        assert handle["secret_refs"]["redis_url"].startswith("secret://proj/")
        assert "redis://default:pw@host:6379" not in str(handle)

        row = (await s.execute(
            select(ProvisionedBackend).where(ProvisionedBackend.project_ref == "proj_q")
        )).scalar_one()
        assert row.provider == "queue" and row.agent_id == "agentQ"

        env = await bp.runtime_env(s, "t_q", "p_q", agent_id="agentQ")
        assert env["REDIS_URL"].startswith("secret://proj/")
