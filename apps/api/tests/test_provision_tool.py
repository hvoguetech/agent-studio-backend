"""Agent self-provisioning tool (#6 slice 2): the `provision_resource` builtin.

An agent, mid-run, provisions an ISOLATED resource for the end user it is serving — gated on the
run's governed subject (ctx.agent_id == the ApiKey the run acts as): its `backend:provision`
capability + per-subject capacity cap, scoped to (agent_id, ctx.end_user). Providers are faked (a
fake ResourceProvider via monkeypatched `get_provider`) so the REAL provision_resource path runs
offline; the injection assertions reuse build_compile_context (per-end-user isolation 2b).
"""

from __future__ import annotations

import json

import pytest

from ros.db.base import SessionLocal
from ros.engine.context import CompileContext
from ros.services import backend_provisioning as bp
from ros.services.apikeys import ApiKeyService
from ros.services.providers.base import ProvisionOutcome
from ros.services.runtime import build_compile_context
from ros.tools.builtin import build_builtin_tool


# --- a fake provider so the real provision_resource path runs offline --------------------

class _FakeProvider:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def is_enabled(self) -> bool:
        return True

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        # Canonical logical so runtime_env maps it (railway-postgres.database_url -> DATABASE_URL);
        # `name` folded into the value so per-call resources are distinguishable.
        if self.kind == "railway-postgres":
            return ProvisionOutcome(external_id=f"ext_{name}", secrets={"database_url": (f"postgresql://{name}", "db_url")})
        raise bp.ProvisionError(f"fake has no kind {self.kind}")

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        return None


@pytest.fixture
def fake_providers(monkeypatch):
    monkeypatch.setattr(bp, "get_provider", lambda kind: _FakeProvider((kind or "").lower()))


def _tool(ctx: CompileContext):
    return build_builtin_tool({"builtin": "provision_resource", "name": "provision_resource"}, ctx)


def _ctx(tenant: str, *, agent_id: str | None, end_user: dict | None = None) -> CompileContext:
    ctx = CompileContext(tenant_id=tenant, project_id="p")
    ctx.agent_id = agent_id
    ctx.end_user = end_user
    return ctx


async def _mk_key(tenant: str, **kw):
    async with SessionLocal() as s:
        key, _ = await ApiKeyService.create(s, tenant_id=tenant, name="k", **kw)
    return key


# --- 1. the happy path: an agent provisions a per-end-user resource, and the run gets it -----

async def test_agent_self_provisions_for_end_user(fake_providers):
    t = "t_self"
    key = await _mk_key(t, capabilities=["backend:provision"])
    ctx = _ctx(t, agent_id=key.id, end_user={"id": "bob"})

    out = json.loads(await _tool(ctx).coroutine(template="db"))
    assert out["errors"] == [] and len(out["provisioned"]) == 1
    assert out["provisioned"][0]["provider"] == "railway-postgres"
    assert out["provisioned"][0]["template"] == "db"
    assert out["end_user_id"] == "bob"
    # No secret VALUES in the tool's return — only handle metadata.
    assert "secret_refs" not in out["provisioned"][0] and "database_url" not in json.dumps(out)

    # bob's run resolves bob's OWN db; alice (no private resource) gets nothing — no cross-user leak.
    async with SessionLocal() as s:
        env_bob = (await build_compile_context(s, tenant_id=t, project_id="p", agent_id=key.id, end_user={"id": "bob"})).runtime_env
        env_alice = (await build_compile_context(s, tenant_id=t, project_id="p", agent_id=key.id, end_user={"id": "alice"})).runtime_env
    assert env_bob["DATABASE_URL"].startswith("postgresql://")
    assert "DATABASE_URL" not in env_alice


# --- 2. capability gate (default-deny) ---------------------------------------------------

async def test_key_without_capability_is_denied(fake_providers):
    t = "t_nocap"
    key = await _mk_key(t)  # capabilities=[]
    ctx = _ctx(t, agent_id=key.id, end_user={"id": "bob"})

    out = await _tool(ctx).coroutine(template="db")
    assert "backend:provision" in out and "Not permitted" in out
    # nothing was provisioned
    async with SessionLocal() as s:
        assert (await build_compile_context(s, tenant_id=t, project_id="p", agent_id=key.id, end_user={"id": "bob"})).runtime_env == {}


# --- 3. per-subject capacity cap ---------------------------------------------------------

async def test_capacity_cap(fake_providers):
    t = "t_cap"
    key = await _mk_key(t, capabilities=["backend:provision"], budget={"max_backends": 1})
    ctx = _ctx(t, agent_id=key.id, end_user={"id": "bob"})
    tool = _tool(ctx)

    first = json.loads(await tool.coroutine(template="db"))
    assert len(first["provisioned"]) == 1
    second = await tool.coroutine(template="db")
    assert "Cannot provision" in second  # at the per-subject max_backends cap


# --- 4. no governed subject → refused ----------------------------------------------------

async def test_operator_run_cannot_self_provision(fake_providers):
    ctx = _ctx("t_op", agent_id=None, end_user={"id": "bob"})
    out = await _tool(ctx).coroutine(template="db")
    assert "governed subject" in out


# --- 5. single-kind override + unknown-template guard ------------------------------------

async def test_single_kind_and_unknown_template(fake_providers):
    t = "t_kind"
    key = await _mk_key(t, capabilities=["backend:provision"])
    ctx = _ctx(t, agent_id=key.id)
    tool = _tool(ctx)

    ok = json.loads(await tool.coroutine(kind="railway-postgres"))
    assert len(ok["provisioned"]) == 1 and ok["provisioned"][0]["provider"] == "railway-postgres"
    assert ok["end_user_id"] is None  # no end_user on ctx → agent-shared resource

    bad = await tool.coroutine(template="does-not-exist")
    assert "Unknown template" in bad


# --- 6. ctx.agent_id is threaded onto the compile context --------------------------------

async def test_build_compile_context_threads_agent_id():
    async with SessionLocal() as s:
        ctx = await build_compile_context(s, tenant_id="t_thread", project_id="p", agent_id="agentX")
    assert ctx.agent_id == "agentX"
