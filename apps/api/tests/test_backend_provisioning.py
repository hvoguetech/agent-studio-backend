"""backend_provisioning service — happy path, rollback-on-failure, and disabled-when-unconfigured.

The Supabase provider is faked by monkeypatching the `supabase_mgmt` module (no network), so these
exercise the service's own logic: credential → secret:// storage, the ProvisionedBackend row, and
the AC1 rollback (external project deleted, no secrets/row left) when provisioning fails mid-way.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import ros.services.providers.supabase_mgmt as supa
from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import ProvisionedBackend, Secret
from ros.services import backend_provisioning as bp


def _patch_supa(monkeypatch, *, fail_after_create: bool = False, deletes: list | None = None):
    async def create_project(token, *, organization_id, name, db_pass, region, plan="free", client):
        return {"id": "refABC", "status": "COMING_UP"}

    async def wait_until_healthy(token, ref, *, client, timeout_s, interval_s):
        if fail_after_create:
            raise supa.SupabaseTimeout("never healthy")
        return {"id": ref, "status": supa.HEALTHY}

    async def get_api_keys(token, ref, *, client):
        return [
            {"name": "anon", "api_key": "anon_x"},
            {"name": "service_role", "api_key": "svc_secret_x"},
        ]

    async def delete_project(token, ref, *, client):
        if deletes is not None:
            deletes.append(ref)

    monkeypatch.setattr(supa, "create_project", create_project)
    monkeypatch.setattr(supa, "wait_until_healthy", wait_until_healthy)
    monkeypatch.setattr(supa, "get_api_keys", get_api_keys)
    monkeypatch.setattr(supa, "delete_project", delete_project)


async def test_provision_stores_creds_as_refs_and_records_row(monkeypatch):
    monkeypatch.setattr(settings, "supabase_management_token", "tok")
    monkeypatch.setattr(settings, "supabase_default_org_id", "org1")
    deletes: list = []
    _patch_supa(monkeypatch, deletes=deletes)

    async with SessionLocal() as s:
        handle = await bp.provision_backend(
            s, "t_prov1", "p_prov1", agent_id="agentXYZ", spec={"provider": "supabase"}
        )

    assert handle["project_ref"] == "refABC"
    assert handle["status"] == "active"
    assert handle["anon_key"] == "anon_x"  # client-safe key returned
    # service_role plaintext must NEVER appear on the handle — only as a secret:// ref.
    assert "svc_secret_x" not in str(handle)
    assert handle["secret_refs"]["service_role_key"] == "secret://proj/supabase_service_role_key__agentXYZ"
    assert deletes == []  # no rollback on success

    async with SessionLocal() as s:
        row = (await s.execute(
            select(ProvisionedBackend).where(ProvisionedBackend.project_ref == "refABC")
        )).scalar_one()
        assert row.tenant_id == "t_prov1" and row.project_id == "p_prov1"
        assert row.agent_id == "agentXYZ" and row.status == "active"

        names = {
            x.name for x in (await s.execute(
                select(Secret).where(Secret.tenant_id == "t_prov1", Secret.project_id == "p_prov1")
            )).scalars()
        }
        assert "supabase_database_url__agentXYZ" in names
        assert "supabase_service_role_key__agentXYZ" in names
        assert "supabase_anon_key__agentXYZ" in names


async def test_provision_rolls_back_external_project_on_failure(monkeypatch):
    monkeypatch.setattr(settings, "supabase_management_token", "tok")
    monkeypatch.setattr(settings, "supabase_default_org_id", "org1")
    deletes: list = []
    _patch_supa(monkeypatch, fail_after_create=True, deletes=deletes)

    async with SessionLocal() as s:
        with pytest.raises(bp.ProvisionError):
            await bp.provision_backend(s, "t_prov2", "p_prov2", agent_id="a2", spec={"provider": "supabase"})

    assert deletes == ["refABC"]  # AC1: the created project is torn down

    async with SessionLocal() as s:
        rows = (await s.execute(
            select(ProvisionedBackend).where(ProvisionedBackend.project_id == "p_prov2")
        )).scalars().all()
        assert rows == []  # no leaked row
        secs = (await s.execute(
            select(Secret).where(Secret.project_id == "p_prov2")
        )).scalars().all()
        assert secs == []  # failure was before any secret write


async def test_provision_disabled_without_token(monkeypatch):
    monkeypatch.setattr(settings, "supabase_management_token", "")
    async with SessionLocal() as s:
        with pytest.raises(bp.ProvisionError, match="not configured"):
            await bp.provision_backend(s, "t_prov3", "p_prov3", spec={"provider": "supabase"})


async def test_provision_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "supabase_management_token", "tok")
    async with SessionLocal() as s:
        with pytest.raises(bp.ProvisionError, match="unsupported backend provider"):
            await bp.provision_backend(s, "t_prov4", "p_prov4", spec={"provider": "neon"})


async def test_provision_creates_storage_buckets(monkeypatch):
    monkeypatch.setattr(settings, "supabase_management_token", "tok")
    monkeypatch.setattr(settings, "supabase_default_org_id", "org1")
    _patch_supa(monkeypatch)
    created: list = []

    async def create_bucket(service_role_key, *, name, public=False, client):
        created.append(name)
        return {"name": name}

    monkeypatch.setattr(supa, "create_storage_bucket", create_bucket)
    async with SessionLocal() as s:
        handle = await bp.provision_backend(
            s, "t_bkt", "p_bkt", agent_id="ab",
            spec={"provider": "supabase", "storage": {"buckets": ["uploads", "artifacts"]}},
        )
    assert handle["status"] == "active"
    assert handle["config"]["buckets"] == ["uploads", "artifacts"]
    assert created == ["uploads", "artifacts"]


async def test_provision_config_is_best_effort(monkeypatch):
    monkeypatch.setattr(settings, "supabase_management_token", "tok")
    monkeypatch.setattr(settings, "supabase_default_org_id", "org1")
    _patch_supa(monkeypatch)

    async def create_bucket(service_role_key, *, name, public=False, client):
        raise supa.SupabaseError("bucket boom")

    monkeypatch.setattr(supa, "create_storage_bucket", create_bucket)
    async with SessionLocal() as s:
        handle = await bp.provision_backend(
            s, "t_be", "p_be",
            spec={"provider": "supabase", "storage": {"buckets": ["x"]}},
        )
    # A bucket failure must NOT tear down the healthy DB — it's recorded, not fatal.
    assert handle["status"] == "active"
    assert handle["config"]["buckets"] == []
    assert any("bucket x" in e for e in handle["config"]["errors"])


async def test_runtime_env_exposes_agent_resources_and_is_isolated(monkeypatch):
    monkeypatch.setattr(settings, "supabase_management_token", "tok")
    monkeypatch.setattr(settings, "supabase_default_org_id", "org1")
    _patch_supa(monkeypatch)
    async with SessionLocal() as s:
        await bp.provision_backend(s, "t_env", "p_env", agent_id="agentE", spec={"provider": "supabase"})
        env = await bp.runtime_env(s, "t_env", "p_env", agent_id="agentE")
    assert env["DATABASE_URL"] == "secret://proj/supabase_database_url__agentE"
    assert env["SUPABASE_ANON_KEY"] == "secret://proj/supabase_anon_key__agentE"
    assert env["SUPABASE_SERVICE_ROLE_KEY"] == "secret://proj/supabase_service_role_key__agentE"
    assert env["SUPABASE_URL"] == "https://refABC.supabase.co"

    # A different agent gets nothing — resources are isolated per agent.
    async with SessionLocal() as s:
        other = await bp.runtime_env(s, "t_env", "p_env", agent_id="someone-else")
    assert other == {}


async def test_runtime_env_isolates_per_end_user_forUser(monkeypatch):
    """forUser: an agent-shared resource + a private one per end user. At runtime each end user gets
    the shared set OVERRIDDEN by their own private resource, and never sees another user's."""
    monkeypatch.setattr(settings, "supabase_management_token", "tok")
    monkeypatch.setattr(settings, "supabase_default_org_id", "org1")
    _patch_supa(monkeypatch)
    sup = {"provider": "supabase"}
    async with SessionLocal() as s:
        await bp.provision_resource(s, "t_fu", "p_fu", agent_id="agentF", kind="supabase", spec=sup)
        await bp.provision_resource(s, "t_fu", "p_fu", agent_id="agentF", end_user_id="alice", kind="supabase", spec=sup)
        await bp.provision_resource(s, "t_fu", "p_fu", agent_id="agentF", end_user_id="bob", kind="supabase", spec=sup)

        shared = await bp.runtime_env(s, "t_fu", "p_fu", agent_id="agentF")                      # no end user
        alice = await bp.runtime_env(s, "t_fu", "p_fu", agent_id="agentF", end_user_id="alice")
        bob = await bp.runtime_env(s, "t_fu", "p_fu", agent_id="agentF", end_user_id="bob")

    # No end user -> only the agent-shared resource (no per-user hash suffix).
    assert shared["DATABASE_URL"] == "secret://proj/supabase_database_url__agentF"
    # alice sees the shared key OVERRIDDEN by her own private resource...
    assert alice["DATABASE_URL"].startswith("secret://proj/supabase_database_url__agentF__u_")
    assert alice["DATABASE_URL"] != shared["DATABASE_URL"]
    # ...and bob gets a DISTINCT private resource (per-end-user isolation).
    assert bob["DATABASE_URL"].startswith("secret://proj/supabase_database_url__agentF__u_")
    assert bob["DATABASE_URL"] != alice["DATABASE_URL"]

