"""Per-end-user isolation 2a: a run records the governed subject (API key id) it acts as.

`Run.agent_id` is the ApiKey.id when the run is created by an API-key principal, else NULL. It is
the prerequisite for injecting the agent's provisioned per-(agent, end_user) credentials at
dispatch (2b). Covers the deps helper, the service persistence, and the router plumbing on the two
API-key-capable run surfaces (console playground + server-to-server Run API).
"""

from __future__ import annotations

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from ros.db.base import SessionLocal
from ros.deps import CurrentUser, get_current_user, governed_subject_id
from ros.main import create_app
from ros.models import Project, Run, Workflow
from ros.services.apikeys import ApiKeyService
from ros.services.runs import RunService

_WF = {
    "id": "wf_agent_id", "version": 1,
    "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
    "entry_node": "agent",
    "nodes": [
        {"id": "agent", "type": "agent", "config": {"flavor": "agent", "model": "fake:echo", "tools": []}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [{"source": "agent", "target": "end"}],
}


async def _workflow(tenant: str, project: str = "p") -> str:
    async with SessionLocal() as s:
        wf = Workflow(tenant_id=tenant, project_id=project, name="R", executable=_WF, status="active")
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        return wf.id


async def _get(run_id: str) -> Run:
    async with SessionLocal() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


def _client(user: CurrentUser | None = None) -> httpx.AsyncClient:
    app = create_app()
    # No lifespan runs in-process, so hand the run service a checkpointer (run_to_completion /
    # aget_state need one); prod gets this from the lifespan.
    app.state.checkpointer = InMemorySaver()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- deps helper -------------------------------------------------------------------------

def test_governed_subject_id_only_for_api_key_principals():
    assert governed_subject_id(CurrentUser(id="apikey:key_abc", tenant_id="t", role="editor")) == "key_abc"
    # A colon in the key id survives (only the leading `apikey:` marker is stripped).
    assert governed_subject_id(CurrentUser(id="apikey:a:b", tenant_id="t", role="editor")) == "a:b"
    assert governed_subject_id(CurrentUser(id="u1", tenant_id="t", role="owner")) is None
    assert governed_subject_id(CurrentUser(id="service", tenant_id="t", role="editor")) is None
    assert governed_subject_id(CurrentUser(id="system-dev", tenant_id="t", role="owner", is_fallback=True)) is None


# --- service: create_run persists agent_id verbatim --------------------------------------

async def test_create_run_persists_agent_id():
    wf_id = await _workflow("t_svc")
    rs = RunService()
    async with SessionLocal() as s:
        run = await rs.create_run(
            s, tenant_id="t_svc", project_id="p", workflow_id=wf_id,
            input={"messages": []}, source="api", agent_id="key_svc",
        )
        run_id = run.id
    assert (await _get(run_id)).agent_id == "key_svc"


async def test_create_run_agent_id_defaults_to_none():
    wf_id = await _workflow("t_none")
    rs = RunService()
    async with SessionLocal() as s:
        run = await rs.create_run(
            s, tenant_id="t_none", project_id="p", workflow_id=wf_id,
            input={"messages": []}, source="playground",
        )
        run_id = run.id
    assert (await _get(run_id)).agent_id is None


# --- routers: the principal's key id is plumbed through -----------------------------------

async def test_playground_run_records_api_key_governed_subject():
    """Full stack: a real ros_sk_ key over Bearer resolves to `apikey:<id>` in get_current_user,
    and the created run records that key id as its governed subject."""
    tenant = "t_pg"
    async with SessionLocal() as s:
        key, plaintext = await ApiKeyService.create(s, tenant_id=tenant, name="crew")
    wf_id = await _workflow(tenant, project="p")
    async with _client() as c:
        r = await c.post(
            f"/v1/projects/p/workflows/{wf_id}/runs",
            json={"input": {"messages": []}},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert r.status_code == 201, r.text
    assert (await _get(r.json()["id"])).agent_id == key.id


async def test_playground_run_operator_has_no_governed_subject():
    tenant = "t_pg2"
    wf_id = await _workflow(tenant, project="p")
    async with _client(CurrentUser(id="u1", tenant_id=tenant, role="owner", email="u@x")) as c:
        r = await c.post(f"/v1/projects/p/workflows/{wf_id}/runs", json={"input": {"messages": []}})
    assert r.status_code == 201, r.text
    assert (await _get(r.json()["id"])).agent_id is None


async def test_server_to_server_run_records_api_key_governed_subject():
    tenant = "t_api"
    # The server-to-server Run API runs the project's CONFIGURED workflow, so bind one to the project.
    async with SessionLocal() as s:
        wf = Workflow(tenant_id=tenant, project_id="pa", name="R", executable=_WF, status="active")
        s.add(wf)
        await s.flush()
        s.add(Project(id="pa", tenant_id=tenant, name="P", slug="pa", config={"api_workflow_id": wf.id}))
        await s.commit()

    async with _client(CurrentUser(id="apikey:key_api", tenant_id=tenant, role="editor")) as c:
        r = await c.post("/v1/projects/pa/run", json={"input": {"messages": []}, "stream": False})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        run = (
            await s.execute(select(Run).where(Run.tenant_id == tenant).order_by(Run.created_at.desc()))
        ).scalars().first()
    assert run is not None and run.agent_id == "key_api"
