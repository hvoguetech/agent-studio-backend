"""Railway storage provider — S3 bucket + credentials via a fake GraphQL transport, and seam wiring
(runtime_env exposes S3_ENDPOINT + S3 keys as secret refs)."""

from __future__ import annotations

import httpx
from sqlalchemy import select

import ros.services.providers.railway_storage as st_mod
from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import ProvisionedBackend
from ros.services import backend_provisioning as bp
from ros.services.providers.railway_storage import RailwayStorageProvider


def _fake_railway_storage(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        q = _j.loads(req.content)["query"]
        if "projectCreate" in q:
            return httpx.Response(200, json={"data": {"projectCreate": {
                "id": "proj_st", "name": "n",
                "environments": {"edges": [{"node": {"id": "env_st", "name": "production"}}]}}}})
        if "bucketCreate" in q:
            return httpx.Response(200, json={"data": {"bucketCreate": {"id": "bkt_1", "name": "uploads"}}})
        if "bucket(id" in q or "bucket(" in q:
            return httpx.Response(200, json={"data": {"bucket": {
                "endpoint": "https://s3.railway.app", "accessKeyId": "AKIA", "secretAccessKey": "sekret"}}})
        if "projectDelete" in q:
            return httpx.Response(200, json={"data": {"projectDelete": True}})
        return httpx.Response(200, json={"data": {}})

    monkeypatch.setattr(settings, "railway_api_token", "tok")
    monkeypatch.setattr(RailwayStorageProvider, "_client",
                        lambda self: httpx.AsyncClient(base_url=st_mod.RAILWAY_API, transport=httpx.MockTransport(handler)))


async def test_provision_creates_bucket_with_credentials(monkeypatch):
    _fake_railway_storage(monkeypatch)
    outcome = await RailwayStorageProvider().provision(name="agent-st", spec={"bucket": "uploads"})
    assert outcome.external_id == "proj_st"
    assert outcome.endpoint_url == "https://s3.railway.app"
    assert outcome.public["bucket"] == "uploads"
    assert outcome.secrets["s3_access_key_id"][0] == "AKIA"
    assert outcome.secrets["s3_secret_access_key"][0] == "sekret"


async def test_seam_exposes_s3_env_as_secret_refs(monkeypatch):
    _fake_railway_storage(monkeypatch)
    async with SessionLocal() as s:
        handle = await bp.provision_resource(
            s, "t_st", "p_st", agent_id="agentST", kind="railway-storage", spec={"bucket": "uploads"}
        )
        assert handle["provider"] == "railway-storage"
        assert "sekret" not in str(handle)  # secret key never inlined

        row = (await s.execute(
            select(ProvisionedBackend).where(ProvisionedBackend.project_ref == "proj_st")
        )).scalar_one()
        assert row.provider == "railway-storage"

        env = await bp.runtime_env(s, "t_st", "p_st", agent_id="agentST")
        assert env["S3_ENDPOINT"] == "https://s3.railway.app"
        assert env["S3_ACCESS_KEY_ID"].startswith("secret://proj/")
        assert env["S3_SECRET_ACCESS_KEY"].startswith("secret://proj/")
