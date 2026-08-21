"""FreestyleBackend (ROS_EXECUTION_BACKEND=freestyle) — dispatch a run to a Freestyle VM running
the ros runtime; fall back to local when the control service is unconfigured; inherit LocalBackend
for retry/reclaim/scheduler/singleton (shared-Postgres, substrate-agnostic)."""

from __future__ import annotations

import httpx

from ros.config import settings
from ros.execution import freestyle_control
from ros.execution.freestyle import FreestyleBackend
from ros.execution.local import LocalBackend
from ros.execution.registry import _resolve


def test_registry_resolves_freestyle():
    backend = _resolve("freestyle")
    assert isinstance(backend, FreestyleBackend) and backend.name == "freestyle"
    assert isinstance(backend, LocalBackend)  # inherits the periodic/singleton machinery


async def test_submit_dispatches_to_vm_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "freestyle_service_url", "http://svc")
    monkeypatch.setattr(freestyle_control, "is_enabled", lambda: True)
    seen: dict = {}

    async def fake_dispatch(*, run_id, tenant_id, project_id, master_url, run_token, sticky_key=None, public=False, run_context=None, client=None):
        seen.update(run_id=run_id, master_url=master_url, run_token=run_token, sticky_key=sticky_key)
        return {"vm_id": "vm_9"}

    monkeypatch.setattr(freestyle_control, "dispatch_run", fake_dispatch)
    out = await FreestyleBackend().submit(run_id="r1", tenant_id="t1", project_id="p1")
    assert out["status"] == "dispatched" and out["backend"] == "freestyle" and out["vm_id"] == "vm_9"
    # the VM is handed a REAL scoped run token (not the static service token)
    from ros.security import decode_token
    claims = decode_token(seen["run_token"], expected_type="run")
    assert seen["run_id"] == "r1" and claims["sub"] == "r1" and claims["scope"] == "runtime:pull"
    assert seen["sticky_key"] is None  # warm-VM mode off by default -> one VM per run


async def test_submit_falls_back_to_local_when_disabled(monkeypatch):
    monkeypatch.setattr(freestyle_control, "is_enabled", lambda: False)
    called = {"n": 0}

    async def fake_super(self, *, run_id, tenant_id, project_id=None, run_service=None, public=False, run_context=None):
        called["n"] += 1
        return {"run_id": run_id, "status": "queued"}

    monkeypatch.setattr("ros.execution.local.LocalBackend.submit", fake_super)
    out = await FreestyleBackend().submit(run_id="r2", tenant_id="t2", project_id="p2")
    assert called["n"] == 1 and out["status"] == "queued"  # delegated to local


async def test_dispatch_run_posts_the_runner_command(monkeypatch):
    monkeypatch.setattr(settings, "freestyle_service_url", "http://svc")
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        captured["path"] = req.url.path
        captured["body"] = _j.loads(req.content)
        return httpx.Response(202, json={"vm_id": "vm_1"})

    client = httpx.AsyncClient(base_url="http://svc", transport=httpx.MockTransport(handler))
    out = await freestyle_control.dispatch_run(
        run_id="r", tenant_id="t", project_id="p", master_url="http://master",
        run_token="tok", client=client,
    )
    await client.aclose()
    assert out["vm_id"] == "vm_1"
    assert captured["path"] == "/run"
    # Trusted-VM: the VM drives the run against the shared DB (not the manifest ainvoke path).
    assert "python -m ros.runtime drive --run-id r --tenant t --project p" in captured["body"]["command"]
    assert captured["body"]["env"]["ROS_MASTER_URL"] == "http://master"
    # Root VM: lets the claude_code CLI skip permission prompts (--dangerously-skip-permissions).
    assert captured["body"]["env"]["IS_SANDBOX"] == "1"
    assert "stickyKey" not in captured["body"] and "warm" not in captured["body"]  # cold by default


async def test_dispatch_run_carries_public_and_run_context():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        captured["body"] = _j.loads(req.content)
        return httpx.Response(202, json={"vm_id": "v"})

    client = httpx.AsyncClient(base_url="http://svc", transport=httpx.MockTransport(handler))
    await freestyle_control.dispatch_run(
        run_id="r", tenant_id="t", project_id="p", master_url="http://master",
        run_token="tok", public=True, run_context={"end_user": {"id": "u1"}}, client=client,
    )
    await client.aclose()
    assert captured["body"]["command"].endswith("--public")  # embed surface -> VM redacts (H5)
    import json as _j
    assert _j.loads(captured["body"]["env"]["ROS_RUN_CONTEXT"]) == {"end_user": {"id": "u1"}}


async def test_dispatch_run_asks_svc_to_reuse_a_warm_vm_when_sticky():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        captured["body"] = _j.loads(req.content)
        return httpx.Response(202, json={"vm_id": "vm_warm"})

    client = httpx.AsyncClient(base_url="http://svc", transport=httpx.MockTransport(handler))
    await freestyle_control.dispatch_run(
        run_id="r", tenant_id="t", project_id="p", master_url="http://master",
        run_token="tok", sticky_key="wf_agent", client=client,
    )
    await client.aclose()
    assert captured["body"]["stickyKey"] == "wf_agent" and captured["body"]["warm"] is True


async def test_submit_records_executor_on_the_run(monkeypatch):
    import uuid

    from ros.db.base import SessionLocal
    from ros.models import Run, Thread, Workflow

    monkeypatch.setattr(settings, "freestyle_service_url", "http://svc")
    monkeypatch.setattr(freestyle_control, "is_enabled", lambda: True)

    t, p = f"t_{uuid.uuid4().hex[:8]}", f"p_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as s:
        wf = Workflow(tenant_id=t, project_id=p, name="w", executable={"nodes": [], "edges": []}, status="active")
        s.add(wf)
        await s.flush()
        thread = Thread(tenant_id=t, project_id=p, workflow_id=wf.id, lg_thread_id="lg", meta={})
        s.add(thread)
        await s.flush()
        run = Run(tenant_id=t, project_id=p, workflow_id=wf.id, thread_id=thread.id, status="queued", input={})
        s.add(run)
        await s.commit()
        rid = run.id

    async def fake_dispatch(**kw):
        return {"vm_id": "vm_rec"}

    monkeypatch.setattr(freestyle_control, "dispatch_run", fake_dispatch)
    await FreestyleBackend().submit(run_id=rid, tenant_id=t, project_id=p)

    async with SessionLocal() as s:
        run = await s.get(Run, rid)
        assert run.executor == {"driver": "freestyle", "vm_id": "vm_rec"}


async def test_submit_keys_sticky_vm_by_agent_workflow_when_warm(monkeypatch):
    import uuid

    from ros.db.base import SessionLocal
    from ros.models import Run, Thread, Workflow

    monkeypatch.setattr(settings, "freestyle_service_url", "http://svc")
    monkeypatch.setattr(settings, "freestyle_warm_vms", True)
    monkeypatch.setattr(freestyle_control, "is_enabled", lambda: True)

    t, p = f"t_{uuid.uuid4().hex[:8]}", f"p_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as s:
        wf = Workflow(tenant_id=t, project_id=p, name="w", executable={"nodes": [], "edges": []}, status="active")
        s.add(wf)
        await s.flush()
        thread = Thread(tenant_id=t, project_id=p, workflow_id=wf.id, lg_thread_id="lg", meta={})
        s.add(thread)
        await s.flush()
        run = Run(tenant_id=t, project_id=p, workflow_id=wf.id, thread_id=thread.id, status="queued", input={})
        s.add(run)
        await s.commit()
        rid, wid = run.id, wf.id

    seen: dict = {}

    async def fake_dispatch(*, run_id, tenant_id, project_id, master_url, run_token, sticky_key=None, public=False, run_context=None, client=None):
        seen["sticky_key"] = sticky_key
        return {"vm_id": "vm"}

    monkeypatch.setattr(freestyle_control, "dispatch_run", fake_dispatch)
    await FreestyleBackend().submit(run_id=rid, tenant_id=t, project_id=p)
    assert seen["sticky_key"] == wid  # one warm VM per agent (keyed by the workflow id)
