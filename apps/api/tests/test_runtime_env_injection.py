"""Per-end-user isolation 2b: the agent's provisioned resource env is injected into its runs.

`resolved_runtime_env` had zero callers; this wires it into the run pipeline scoped by the run's
governed subject (Run.agent_id) and bound end_user:
- build_compile_context (DB path: master-local + trusted-VM driver) -> ctx.runtime_env
- RuntimeManifestService.build + the manifest endpoint (DB-less VM path) -> manifest["runtime_env"]
- build_compile_context_from_manifest -> ctx.runtime_env
- apply_runtime_env: the VM entrypoint's leak-safe os.environ export

Guardrails covered: no injection without a governed subject (operator runs), per-agent + per-end-user
scoping (no cross-agent / cross-user leak), and warm-VM reconcile in the os.environ export.
"""

from __future__ import annotations

from starlette.requests import Request

from ros.db.base import SessionLocal
from ros.models import ProvisionedBackend, Project, Run, Thread, Workflow
from ros.security import create_run_token
from ros.services.backend_provisioning import runtime_env_for_run
from ros.services.runtime import build_compile_context, build_compile_context_from_manifest
from ros.services.runtime_manifest import RuntimeManifestService
from ros.services.secrets import SecretService


async def _prov(session, tenant, project, agent_id, *, endpoint_url=None, end_user_id=None,
                provider="railway", secret_refs=None):
    session.add(ProvisionedBackend(
        tenant_id=tenant, project_id=project, agent_id=agent_id, end_user_id=end_user_id,
        provider=provider, status="active", endpoint_url=endpoint_url, secret_refs=secret_refs or {},
    ))
    await session.commit()


def _req(token: str) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""})


# --- apply_runtime_env: the VM-only os.environ export -------------------------------------

def test_apply_runtime_env_sets_skips_nonstr_and_reconciles():
    import os

    from ros.runtime.env import apply_runtime_env
    try:
        applied = apply_runtime_env({"ZZ_DB": "u1", "ZZ_REDIS": "r1", "ZZ_BAD": 123})
        assert applied == ["ZZ_DB", "ZZ_REDIS"]  # non-str value skipped
        assert os.environ["ZZ_DB"] == "u1" and os.environ["ZZ_REDIS"] == "r1"
        assert "ZZ_BAD" not in os.environ
        # A later run on a REUSED (warm) process: the stale key is removed and the overlap overwritten,
        # so one run's creds can't bleed into the next on the same VM.
        apply_runtime_env({"ZZ_DB": "u2"})
        assert os.environ["ZZ_DB"] == "u2"
        assert "ZZ_REDIS" not in os.environ
    finally:
        apply_runtime_env(None)  # reconcile everything this test set back out
        assert "ZZ_DB" not in os.environ and "ZZ_REDIS" not in os.environ


# --- build_compile_context (DB path) -----------------------------------------------------

async def test_build_compile_context_injects_runtime_env_scoped_to_agent_and_end_user():
    t, p = "t_ci", "p_ci"
    async with SessionLocal() as s:
        await _prov(s, t, p, "agentA", endpoint_url="https://shared.example")
        await _prov(s, t, p, "agentA", end_user_id="alice", endpoint_url="https://alice.example")

    async with SessionLocal() as s:
        # alice sees her own resource overriding the agent-shared one
        ctx = await build_compile_context(s, tenant_id=t, project_id=p, agent_id="agentA",
                                          end_user={"id": "alice"})
        assert ctx.runtime_env["SERVICE_URL"] == "https://alice.example"
        # no end user -> only the agent-shared resource
        ctx_shared = await build_compile_context(s, tenant_id=t, project_id=p, agent_id="agentA")
        assert ctx_shared.runtime_env["SERVICE_URL"] == "https://shared.example"
        # a different agent gets nothing (cross-agent isolation)
        ctx_other = await build_compile_context(s, tenant_id=t, project_id=p, agent_id="agentB")
        assert ctx_other.runtime_env == {}


async def test_build_compile_context_no_injection_for_operator_run():
    t, p = "t_op", "p_op"
    async with SessionLocal() as s:
        await _prov(s, t, p, "agentA", endpoint_url="https://x.example")
    async with SessionLocal() as s:
        ctx = await build_compile_context(s, tenant_id=t, project_id=p)  # agent_id=None
        assert ctx.runtime_env == {}


async def test_build_compile_context_resolves_secret_refs_to_values():
    t, p = "t_sec", "p_sec"
    async with SessionLocal() as s:
        await SecretService.write(s, t, p, name="agentS_db", value="postgresql://real/db")
        await _prov(s, t, p, "agentS", provider="railway-postgres",
                    secret_refs={"database_url": "secret://proj/agentS_db"})
    async with SessionLocal() as s:
        ctx = await build_compile_context(s, tenant_id=t, project_id=p, agent_id="agentS")
        assert ctx.runtime_env["DATABASE_URL"] == "postgresql://real/db"  # ref resolved to value


# --- runtime_env_for_run (VM drive entrypoint resolver) ----------------------------------

async def test_runtime_env_for_run_uses_run_agent_and_thread_end_user():
    t, p = "t_rr", "p_rr"
    async with SessionLocal() as s:
        await _prov(s, t, p, "agentA", endpoint_url="https://shared.example")
        await _prov(s, t, p, "agentA", end_user_id="bob", endpoint_url="https://bob.example")
        th = Thread(tenant_id=t, project_id=p, workflow_id="w", lg_thread_id=f"{t}:x",
                    meta={"end_user": {"id": "bob"}})
        s.add(th)
        await s.flush()
        run = Run(tenant_id=t, project_id=p, workflow_id="w", thread_id=th.id, status="queued",
                  agent_id="agentA")
        s.add(run)
        await s.commit()
        run_id = run.id

    async with SessionLocal() as s:
        env = await runtime_env_for_run(s, run_id=run_id, tenant_id=t)
    assert env["SERVICE_URL"] == "https://bob.example"  # scoped to the run's agent + bound end_user


async def test_runtime_env_for_run_empty_for_operator_run():
    t, p = "t_rro", "p_rro"
    async with SessionLocal() as s:
        await _prov(s, t, p, "agentA", endpoint_url="https://x.example")
        th = Thread(tenant_id=t, project_id=p, workflow_id="w", lg_thread_id=f"{t}:y", meta={})
        s.add(th)
        await s.flush()
        run = Run(tenant_id=t, project_id=p, workflow_id="w", thread_id=th.id, status="queued")  # agent_id=None
        s.add(run)
        await s.commit()
        run_id = run.id
    async with SessionLocal() as s:
        assert await runtime_env_for_run(s, run_id=run_id, tenant_id=t) == {}


# --- manifest build + endpoint (DB-less VM path) -----------------------------------------

async def _project_and_workflow(s, t, p_slug):
    proj = Project(tenant_id=t, name="P", slug=p_slug, config={})
    s.add(proj)
    await s.flush()
    wf = Workflow(tenant_id=t, project_id=proj.id, name="f", executable={"nodes": [], "edges": []})
    s.add(wf)
    await s.flush()
    return proj, wf


async def test_manifest_includes_scoped_runtime_env():
    t = "t_man"
    async with SessionLocal() as s:
        proj, wf = await _project_and_workflow(s, t, "p-man")
        await _prov(s, t, proj.id, "agentA", endpoint_url="https://shared.example")
        await _prov(s, t, proj.id, "agentA", end_user_id="carol", endpoint_url="https://carol.example")
        await s.commit()

        m_scoped = await RuntimeManifestService.build(
            s, tenant_id=t, project_id=proj.id, workflow_id=wf.id, agent_id="agentA", end_user_id="carol")
        assert m_scoped["runtime_env"]["SERVICE_URL"] == "https://carol.example"
        # without a governed subject the manifest carries no provisioned env
        m_plain = await RuntimeManifestService.build(s, tenant_id=t, project_id=proj.id, workflow_id=wf.id)
        assert m_plain["runtime_env"] == {}


def test_build_compile_context_from_manifest_reads_runtime_env():
    manifest = {
        "tenant_id": "t", "project_id": "p", "executable": {"nodes": [], "edges": []},
        "runtime_env": {"DATABASE_URL": "postgresql://x", "SERVICE_URL": "https://y"},
    }
    ctx = build_compile_context_from_manifest(manifest)
    assert ctx.runtime_env == {"DATABASE_URL": "postgresql://x", "SERVICE_URL": "https://y"}
    # a manifest with no runtime_env yields an empty (never None) dict
    assert build_compile_context_from_manifest({"tenant_id": "t", "project_id": "p", "executable": {}}).runtime_env == {}


async def test_manifest_endpoint_scopes_runtime_env_to_the_run():
    from ros.routers.runtime import get_run_manifest

    t = "t_ep"
    async with SessionLocal() as s:
        proj, wf = await _project_and_workflow(s, t, "p-ep")
        await _prov(s, t, proj.id, "agentA", endpoint_url="https://shared.example")
        await _prov(s, t, proj.id, "agentA", end_user_id="dave", endpoint_url="https://dave.example")
        th = Thread(tenant_id=t, project_id=proj.id, workflow_id=wf.id, lg_thread_id=f"{t}:z",
                    meta={"end_user": {"id": "dave"}})
        s.add(th)
        await s.flush()
        run = Run(tenant_id=t, project_id=proj.id, workflow_id=wf.id, thread_id=th.id, status="queued",
                  agent_id="agentA")
        s.add(run)
        await s.commit()
        rid = run.id

        tok = create_run_token(run_id=rid, tenant_id=t, project_id=proj.id)
        manifest = await get_run_manifest(rid, _req(tok), session=s)
    # The VM pulling this run's manifest gets exactly the run's (agent, end_user)-scoped env.
    assert manifest["runtime_env"]["SERVICE_URL"] == "https://dave.example"


async def test_manifest_endpoint_no_runtime_env_for_operator_run():
    from ros.routers.runtime import get_run_manifest

    t = "t_epo"
    async with SessionLocal() as s:
        proj, wf = await _project_and_workflow(s, t, "p-epo")
        await _prov(s, t, proj.id, "agentA", endpoint_url="https://x.example")
        th = Thread(tenant_id=t, project_id=proj.id, workflow_id=wf.id, lg_thread_id=f"{t}:q", meta={})
        s.add(th)
        await s.flush()
        run = Run(tenant_id=t, project_id=proj.id, workflow_id=wf.id, thread_id=th.id, status="queued")  # no agent
        s.add(run)
        await s.commit()
        rid = run.id
        tok = create_run_token(run_id=rid, tenant_id=t, project_id=proj.id)
        manifest = await get_run_manifest(rid, _req(tok), session=s)
    assert manifest["runtime_env"] == {}
