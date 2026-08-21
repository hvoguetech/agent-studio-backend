"""SandboxBackend (ROS_EXECUTION_BACKEND=sandbox) — the ISOLATING data-plane path (WS10 Phase 1).

The sandbox holds NO shared DB/Redis/master-key: it gets only ROS_MASTER_URL + a run-scoped token and
reads/writes everything through master's runtime callback API. These tests pin that boundary + the
dispatch shape + the callback endpoints (auth + behavior).
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ros.config import settings
from ros.execution import freestyle_control
from ros.execution.local import LocalBackend
from ros.execution.registry import _resolve
from ros.execution.sandbox import SandboxBackend
from ros.security import create_run_token


def test_registry_resolves_sandbox():
    backend = _resolve("sandbox")
    assert isinstance(backend, SandboxBackend) and backend.name == "sandbox"
    assert isinstance(backend, LocalBackend)  # inherits retry/reclaim/scheduler/singleton


async def test_sandbox_dispatch_omits_all_shared_creds():
    """THE isolation invariant: the dispatched env carries ONLY the master URL + run token — never
    ROS_DATABASE_URL / ROS_REDIS_URL / ROS_SECRET_KEY (contrast the trusted-VM dispatch_run)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(202, json={"vm_id": "sbx_1"})

    client = httpx.AsyncClient(base_url="http://svc", transport=httpx.MockTransport(handler))
    out = await freestyle_control.dispatch_sandbox_run(
        run_id="r", tenant_id="t", project_id="p", master_url="http://master",
        run_token="tok", run_input={"messages": []}, client=client,
    )
    await client.aclose()
    assert out["vm_id"] == "sbx_1"
    env = captured["body"]["env"]
    # Master URL + token + the non-secret IS_SANDBOX execution-mode flag — and NOTHING else.
    assert env == {"ROS_MASTER_URL": "http://master", "ROS_RUNTIME_TOKEN": "tok", "IS_SANDBOX": "1"}
    # THE isolation invariant: no shared creds leak into the sandbox env.
    for forbidden in ("ROS_DATABASE_URL", "ROS_REDIS_URL", "ROS_SECRET_KEY", "ROS_CHECKPOINT_POSTGRES_URL"):
        assert forbidden not in env
    cmd = captured["body"]["command"]
    assert "python -m ros.runtime sandbox --run-id r" in cmd
    assert "--master-url http://master" in cmd and "--token tok" in cmd


async def test_sandbox_dispatch_passes_input_and_public():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(202, json={"vm_id": "v"})

    client = httpx.AsyncClient(base_url="http://svc", transport=httpx.MockTransport(handler))
    await freestyle_control.dispatch_sandbox_run(
        run_id="r", tenant_id="t", project_id="p", master_url="http://m",
        run_token="tok", run_input={"messages": [{"role": "user", "content": "hi there"}]},
        public=True, client=client,
    )
    await client.aclose()
    cmd = captured["body"]["command"]
    assert cmd.endswith("--public")
    assert "--input" in cmd  # the run input is shell-quoted into the command


async def test_submit_falls_back_to_local_when_disabled(monkeypatch):
    monkeypatch.setattr(freestyle_control, "is_enabled", lambda: False)
    called = {"n": 0}

    async def fake_super(self, *, run_id, tenant_id, project_id=None, run_service=None, public=False, run_context=None):
        called["n"] += 1
        return {"run_id": run_id, "status": "queued"}

    monkeypatch.setattr("ros.execution.local.LocalBackend.submit", fake_super)
    out = await SandboxBackend().submit(run_id="r2", tenant_id="t2", project_id="p2")
    assert called["n"] == 1 and out["status"] == "queued"


async def test_submit_dispatches_sandbox_with_scoped_token(monkeypatch):
    monkeypatch.setattr(settings, "freestyle_service_url", "http://svc")
    monkeypatch.setattr(freestyle_control, "is_enabled", lambda: True)
    seen: dict = {}

    async def fake_dispatch(*, run_id, tenant_id, project_id, master_url, run_token, run_input=None,
                            sticky_key=None, public=False, run_context=None, client=None):
        seen.update(run_id=run_id, run_token=run_token, run_input=run_input)
        return {"vm_id": "sbx_9"}

    async def fake_input(self, run_id, tenant_id):
        return {"messages": []}

    async def fake_record(self, run_id, tenant_id, vm_id):
        return None

    monkeypatch.setattr(freestyle_control, "dispatch_sandbox_run", fake_dispatch)
    monkeypatch.setattr(SandboxBackend, "_run_input", fake_input)
    monkeypatch.setattr(SandboxBackend, "_record_executor", fake_record)
    out = await SandboxBackend().submit(run_id="r1", tenant_id="t1", project_id="p1")
    assert out["status"] == "dispatched" and out["backend"] == "sandbox" and out["vm_id"] == "sbx_9"
    from ros.security import decode_token
    claims = decode_token(seen["run_token"], expected_type="run")
    assert claims["sub"] == "r1" and claims["scope"] == "runtime:pull"


# --- callback endpoints -------------------------------------------------------------------------


def _req(token: str) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers, "query_string": b""})


async def test_frames_callback_relays_to_bus(monkeypatch):
    from ros.routers.runtime import FramesIn, post_run_frames

    published: list = []

    async def fake_publish(run_id, seq, frame, *, tenant_id=""):
        published.append((run_id, seq, frame["event"], tenant_id))

    monkeypatch.setattr("ros.routers.runtime.publish_frame", fake_publish)
    tok = create_run_token(run_id="r1", tenant_id="t1", project_id="p1")
    body = FramesIn(frames=[{"seq": 1, "event": "node_start", "data": {"node": "a"}},
                            {"seq": 2, "event": "done", "data": {}}])
    out = await post_run_frames("r1", body, _req(tok))
    assert out == {"ok": True, "count": 2}
    # tenant comes from the TOKEN, not the body
    assert published == [("r1", 1, "node_start", "t1"), ("r1", 2, "done", "t1")]


async def test_callback_rejects_token_for_a_different_run():
    from ros.routers.runtime import FramesIn, post_run_frames

    tok = create_run_token(run_id="r_a", tenant_id="t", project_id="p")
    with pytest.raises(HTTPException) as ei:
        await post_run_frames("r_b", FramesIn(frames=[]), _req(tok))
    assert ei.value.status_code == 403


async def test_callback_rejects_garbage_token():
    from ros.routers.runtime import FramesIn, post_run_frames

    with pytest.raises(HTTPException) as ei:
        await post_run_frames("r", FramesIn(frames=[]), _req("garbage"))
    assert ei.value.status_code == 401


async def test_result_persists_terminal_state():
    from ros.db.base import SessionLocal
    from ros.models import Project, Run, Workflow
    from ros.routers.runtime import ResultIn, post_run_result

    async with SessionLocal() as s:
        proj = Project(tenant_id="t_sb", name="p", slug="p-sb", config={})
        s.add(proj)
        await s.flush()
        wf = Workflow(tenant_id="t_sb", project_id=proj.id, name="f", executable={"nodes": [], "edges": []})
        s.add(wf)
        await s.flush()
        run = Run(tenant_id="t_sb", project_id=proj.id, workflow_id=wf.id, thread_id="th_sb", status="running")
        s.add(run)
        await s.flush()
        rid = run.id
        await s.commit()

        tok = create_run_token(run_id=rid, tenant_id="t_sb", project_id=proj.id)
        body = ResultIn(status="done", output={"messages": []}, total_tokens=42, total_cost_usd=0.001)
        out = await post_run_result(rid, body, _req(tok), session=s)
        assert out == {"ok": True, "status": "done"}
        refreshed = await s.get(Run, rid)
        assert refreshed.status == "done" and refreshed.total_tokens == 42 and refreshed.ended_at is not None


async def test_status_callback_rejects_terminal():
    from ros.db.base import SessionLocal
    from ros.routers.runtime import StatusIn, post_run_status

    tok = create_run_token(run_id="r_term", tenant_id="t", project_id="p")
    async with SessionLocal() as s:
        with pytest.raises(HTTPException) as ei:
            await post_run_status("r_term", StatusIn(status="done"), _req(tok), session=s)
    assert ei.value.status_code == 400  # terminal must go via /result
