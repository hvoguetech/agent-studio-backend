"""ApiKey as the governed subject (agent-profile merge): capability allow-list + spend cap + the
resources it owns (ProvisionedBackend rows keyed by the key's id)."""

from __future__ import annotations

import pytest

from ros.db.base import SessionLocal
from ros.models import ProvisionedBackend
from ros.services.apikeys import ApiKeyService
from ros.services.budget import ProvisionNotAllowed


async def test_key_carries_capabilities_and_default_deny():
    async with SessionLocal() as s:
        key, plaintext = await ApiKeyService.create(
            s, tenant_id="t_k", name="crew", capabilities=["backend:provision"],
            budget={"max_backends": 1},
        )
    assert plaintext.startswith("ros_sk_")
    assert ApiKeyService.allows(key, "backend:provision") is True
    assert ApiKeyService.allows(key, "backend:delete") is False  # not on the list -> denied


async def test_wildcard_capability_allows_all():
    async with SessionLocal() as s:
        key, _ = await ApiKeyService.create(s, tenant_id="t_k", name="admin", capabilities=["*"])
    assert ApiKeyService.allows(key, "anything:at:all") is True


async def test_key_owns_resources_runtime_env_and_capacity_cap():
    async with SessionLocal() as s:
        key, _ = await ApiKeyService.create(
            s, tenant_id="t_ko", name="crew", project_id="p_ko", budget={"max_backends": 1},
        )
        await ApiKeyService.enforce_capacity(s, key)  # 0 owned -> ok

        s.add(ProvisionedBackend(
            tenant_id="t_ko", project_id="p_ko", agent_id=key.id, provider="railway-postgres",
            status="active", secret_refs={"database_url": f"secret://proj/railway-postgres_database_url__{key.id}"},
        ))
        await s.commit()

        env = await ApiKeyService.runtime_env(s, key)
        assert env["DATABASE_URL"].startswith("secret://proj/")

        with pytest.raises(ProvisionNotAllowed):
            await ApiKeyService.enforce_capacity(s, key)  # at cap
