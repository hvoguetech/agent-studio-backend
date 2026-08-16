"""Provisioning DX (#6 slice 1): templates + provision/list/teardown routes, and the end-to-end
provision -> inject -> run loop with per-end-user isolation.

Providers are faked (a fake ResourceProvider via monkeypatched `get_provider`) so the REAL
`provision_resource` runs — secret storage, the ProvisionedBackend row, env-var mapping, teardown —
without touching Railway/Supabase. The injection assertions reuse build_compile_context (#3).
"""

from __future__ import annotations

import httpx
import pytest

from ros.deps import CurrentUser, get_current_user
from ros.main import create_app
from ros.services import backend_provisioning as bp
from ros.services.apikeys import ApiKeyService
from ros.services.providers.base import ProvisionOutcome
from ros.services.runtime import build_compile_context
from ros.db.base import SessionLocal


# --- a fake provider so the real provision_resource path runs offline --------------------

class _FakeProvider:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def is_enabled(self) -> bool:
        return True

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        # Secrets keyed to the CANONICAL logicals so runtime_env maps them (railway-postgres.database_url
        # -> DATABASE_URL, etc.). `name` is folded into values so per-call resources are distinguishable.
        if self.kind == "railway-postgres":
            return ProvisionOutcome(external_id=f"ext_{name}", secrets={"database_url": (f"postgresql://{name}", "db_url")})
        if self.kind == "railway-storage":
            return ProvisionOutcome(external_id=f"ext_{name}", endpoint_url="https://s3.example",
                                    secrets={"s3_access_key_id": (f"AK_{name}", "s3"), "s3_secret_access_key": (f"SK_{name}", "s3")})
        if self.kind == "queue":
            return ProvisionOutcome(external_id=f"ext_{name}", secrets={"redis_url": (f"rediss://{name}", "redis")})
        raise bp.ProvisionError(f"fake has no kind {self.kind}")

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        return None


@pytest.fixture
def fake_providers(monkeypatch):
    monkeypatch.setattr(bp, "get_provider", lambda kind: _FakeProvider((kind or "").lower()))


def _client(user: CurrentUser) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _op(tenant: str, role: str = "editor") -> CurrentUser:
    return CurrentUser(id="u_op", tenant_id=tenant, role=role, email="op@x")


# --- 1. templates catalog ----------------------------------------------------------------

async def test_list_templates(fake_providers):
    async with _client(_op("t_tpl")) as c:
        r = await c.get("/v1/projects/p/provisioning/templates")
    assert r.status_code == 200, r.text
    by_id = {t["id"]: t for t in r.json()}
    assert set(by_id) == {"db", "db+storage", "db+storage+queue"}
    assert by_id["db"]["resources"] == ["railway-postgres"]
    assert by_id["db"]["enabled"] is True  # fake providers all enabled


# --- 2/3. provision a template + the run gets the creds injected --------------------------

async def test_provision_template_then_run_gets_injected(fake_providers):
    t = "t_prov"
    async with _client(_op(t)) as c:
        r = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db", "agent_id": "agentA"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == [] and len(body["provisioned"]) == 1
    assert body["provisioned"][0]["provider"] == "railway-postgres"
    assert body["provisioned"][0]["template"] == "db"
    # secret VALUE never leaves the store — only a ref
    assert body["provisioned"][0]["secret_refs"]["database_url"].startswith("secret://")

    # A keyed run for agentA resolves agentA's own DATABASE_URL (the #3 injection path).
    async with SessionLocal() as s:
        ctx = await build_compile_context(s, tenant_id=t, project_id="p", agent_id="agentA")
    assert ctx.runtime_env["DATABASE_URL"].startswith("postgresql://")


# --- 4. per-end-user isolation (the forUser wedge) ---------------------------------------

async def test_two_end_users_get_isolated_resources(fake_providers):
    t = "t_fu"
    async with _client(_op(t)) as c:
        shared = await c.post("/v1/projects/p/provisioning/provision",
                              json={"template": "db", "agent_id": "agentA", "name": "shared-db"})
        bob = await c.post("/v1/projects/p/provisioning/provision",
                           json={"template": "db", "agent_id": "agentA", "end_user_id": "bob", "name": "bob-db"})
    assert shared.status_code == 200 and bob.status_code == 200, (shared.text, bob.text)

    async with SessionLocal() as s:
        env_bob = (await build_compile_context(s, tenant_id=t, project_id="p", agent_id="agentA",
                                               end_user={"id": "bob"})).runtime_env
        env_alice = (await build_compile_context(s, tenant_id=t, project_id="p", agent_id="agentA",
                                                 end_user={"id": "alice"})).runtime_env
        env_shared = (await build_compile_context(s, tenant_id=t, project_id="p", agent_id="agentA")).runtime_env

    # bob sees his OWN db; alice (no private resource) falls back to the agent-shared db; they differ.
    assert env_bob["DATABASE_URL"] == "postgresql://bob-db"
    assert env_alice["DATABASE_URL"] == "postgresql://shared-db" == env_shared["DATABASE_URL"]
    assert env_bob["DATABASE_URL"] != env_alice["DATABASE_URL"]  # no cross-user leak


# --- 5. list + teardown ------------------------------------------------------------------

async def test_list_and_teardown(fake_providers):
    t = "t_td"
    async with _client(_op(t)) as c:
        prov = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db", "agent_id": "agentA"})
        backend_id = prov.json()["provisioned"][0]["backend_id"]

        listed = await c.get("/v1/projects/p/provisioning/resources", params={"agent_id": "agentA"})
        assert listed.status_code == 200
        assert [x["backend_id"] for x in listed.json()] == [backend_id]

        gone = await c.delete(f"/v1/projects/p/provisioning/resources/{backend_id}")
        assert gone.status_code == 200, gone.text

        after = await c.get("/v1/projects/p/provisioning/resources", params={"agent_id": "agentA"})
        assert after.json() == []

    # nothing injects for a torn-down resource
    async with SessionLocal() as s:
        assert (await build_compile_context(s, tenant_id=t, project_id="p", agent_id="agentA")).runtime_env == {}


# --- 6. authz + capability guardrails ----------------------------------------------------

async def test_viewer_cannot_provision(fake_providers):
    async with _client(_op("t_v", role="viewer")) as c:
        r = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db", "agent_id": "agentA"})
    assert r.status_code == 403


async def test_operator_must_name_agent_id(fake_providers):
    async with _client(_op("t_na")) as c:
        r = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db"})
    assert r.status_code == 400


async def test_api_key_needs_capability_and_self_scopes(fake_providers):
    t = "t_cap"
    async with SessionLocal() as s:
        denied, _ = await ApiKeyService.create(s, tenant_id=t, name="no-cap")               # capabilities=[]
        allowed, _ = await ApiKeyService.create(s, tenant_id=t, name="prov", capabilities=["backend:provision"])

    # A key WITHOUT the capability is denied (default-deny).
    async with _client(CurrentUser(id=f"apikey:{denied.id}", tenant_id=t, role="editor")) as c:
        r = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db"})
    assert r.status_code == 403

    # A key WITH it self-provisions for its OWN id; naming another agent is rejected.
    async with _client(CurrentUser(id=f"apikey:{allowed.id}", tenant_id=t, role="editor")) as c:
        ok = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["agent_id"] == allowed.id  # scoped to the key's governed subject
        other = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db", "agent_id": "someone-else"})
        assert other.status_code == 403


async def test_api_key_capacity_cap(fake_providers):
    t = "t_capacity"
    async with SessionLocal() as s:
        key, _ = await ApiKeyService.create(s, tenant_id=t, name="capped",
                                            capabilities=["backend:provision"], budget={"max_backends": 1})
    async with _client(CurrentUser(id=f"apikey:{key.id}", tenant_id=t, role="editor")) as c:
        first = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db"})
        assert first.status_code == 200, first.text
        second = await c.post("/v1/projects/p/provisioning/provision", json={"template": "db"})
    assert second.status_code == 402  # at the per-subject max_backends cap
