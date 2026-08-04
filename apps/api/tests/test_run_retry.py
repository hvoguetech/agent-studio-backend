"""A/C11 - operator retry of a terminal run (resume from checkpoint | restart on latest).

Covers the two modes at the service/backend layer (AC-2 not-resumable signal, AC-3 restart +
lineage) and the endpoint's role gate + audit trail (AC-1, AC-4). The resume-completes path
reuses _continue_from_checkpoint, covered by test_execution_backend / test_run_reclaim.
"""

from __future__ import annotations

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from ros.db.base import SessionLocal
from ros.deps import CurrentUser, get_current_user
from ros.execution.local import LocalBackend
from ros.main import create_app
from ros.models import Run, Workflow
from ros.services.runs import RunService

_WF = {
    "id": "wf_retry", "version": 1,
    "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
    "entry_node": "agent",
    "nodes": [
        {"id": "agent", "type": "agent", "config": {"flavor": "agent", "model": "fake:echo"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [{"source": "agent", "target": "end"}],
}


async def _get(run_id: str) -> Run:
    async with SessionLocal() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


async def _workflow(tenant: str, project: str = "p") -> str:
    async with SessionLocal() as s:
        wf = Workflow(tenant_id=tenant, project_id=project, name="R", executable=_WF, status="active")
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        return wf.id


def _client_as(role: str, tenant: str = "t_ep"):
    app = create_app()

    async def _user() -> CurrentUser:
        return CurrentUser(id="u1", tenant_id=tenant, role=role, email="u@x")

    app.dependency_overrides[get_current_user] = _user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- AC-3: restart creates a fresh run on a new thread, with lineage ----------------------

async def test_restart_creates_fresh_run_with_lineage():
    wf_id = await _workflow("t_rs")
    rs = RunService(checkpointer=InMemorySaver())
    async with SessionLocal() as s:
        orig = await rs.create_run(
            s, tenant_id="t_rs", project_id="p", workflow_id=wf_id,
            input={"messages": [{"role": "user", "content": "hi"}]}, source="playground",
        )
        orig_id, orig_thread = orig.id, orig.thread_id

    res = await LocalBackend().retry(
        run_id=orig_id, tenant_id="t_rs", mode="restart", project_id="p", run_service=rs
    )
    assert res["retry_of"] == orig_id
    assert res["run_id"] != orig_id
    assert res["thread_id"] != orig_thread  # fresh thread
    new = await _get(res["run_id"])
    assert new.source == "retry"
    assert new.status == "queued"


# --- AC-2: resume of a run with no resumable checkpoint returns the 409 signal -------------

async def test_resume_not_resumable_returns_signal():
    wf_id = await _workflow("t_nr")
    rs = RunService(checkpointer=InMemorySaver())
    async with SessionLocal() as s:
        run = await rs.create_run(
            s, tenant_id="t_nr", project_id="p", workflow_id=wf_id,
            input={"messages": [{"role": "user", "content": "hi"}]}, source="test",
        )
        run_id = run.id
    assert (await rs.run_to_completion(run_id=run_id, tenant_id="t_nr", project_id="p"))["status"] == "done"
    # A completed thread has no pending work; mark the run 'error' so it is retry-eligible by status.
    async with SessionLocal() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        run.status = "error"
        await s.commit()
    res = await rs.retry_resume(run_id=run_id, tenant_id="t_nr", project_id="p")
    assert res["status"] == "not_resumable"


async def test_resume_rejects_non_terminal_run():
    async with SessionLocal() as s:
        run = Run(tenant_id="t_nt", project_id="p", workflow_id="w", thread_id="th", status="done", input={})
        s.add(run)
        await s.commit()
        await s.refresh(run)
        run_id = run.id
    rs = RunService(checkpointer=InMemorySaver())
    res = await rs.retry_resume(run_id=run_id, tenant_id="t_nt", project_id="p")
    assert "not retryable" in (res.get("error") or "")


# --- AC-1: the endpoint is editor+ gated (403 below), reachable to an operator ------------

async def test_retry_endpoint_requires_editor_role():
    async with _client_as("viewer") as c:
        r = await c.post("/v1/projects/p_ep/workflows/w/runs/nope/retry", json={"mode": "resume"})
        assert r.status_code == 403
    async with _client_as("editor") as c:
        r = await c.post("/v1/projects/p_ep/workflows/w/runs/nope/retry", json={"mode": "resume"})
        assert r.status_code == 404  # passes the role gate; the run just doesn't exist


# --- AC-4: a successful retry is audit-logged (actor + mode + lineage) ---------------------

async def test_retry_is_audit_logged():
    wf_id = await _workflow("t_ep2", project="p_ep2")
    rs = RunService(checkpointer=InMemorySaver())
    async with SessionLocal() as s:
        orig = await rs.create_run(
            s, tenant_id="t_ep2", project_id="p_ep2", workflow_id=wf_id,
            input={"messages": [{"role": "user", "content": "hi"}]}, source="playground",
        )
        orig_id = orig.id
    async with _client_as("editor", tenant="t_ep2") as c:
        r = await c.post(f"/v1/projects/p_ep2/workflows/{wf_id}/runs/{orig_id}/retry", json={"mode": "restart"})
        assert r.status_code == 200
        assert r.json()["retry_of"] == orig_id

    from ros.services.audit import AuditService

    async with SessionLocal() as s:
        rows = await AuditService.recent(s, "t_ep2")
    assert any(row.action == "run.retry" for row in rows)
