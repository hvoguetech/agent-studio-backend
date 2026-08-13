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

    async def fake_dispatch(*, run_id, tenant_id, project_id, master_url, run_token, client=None):
        seen.update(run_id=run_id, master_url=master_url, run_token=run_token)
        return {"vm_id": "vm_9"}

    monkeypatch.setattr(freestyle_control, "dispatch_run", fake_dispatch)
    out = await FreestyleBackend().submit(run_id="r1", tenant_id="t1", project_id="p1")
    assert out["status"] == "dispatched" and out["backend"] == "freestyle" and out["vm_id"] == "vm_9"
    # the VM is handed a REAL scoped run token (not the static service token)
    from ros.security import decode_token
    claims = decode_token(seen["run_token"], expected_type="run")
    assert seen["run_id"] == "r1" and claims["sub"] == "r1" and claims["scope"] == "runtime:pull"


async def test_submit_falls_back_to_local_when_disabled(monkeypatch):
    monkeypatch.setattr(freestyle_control, "is_enabled", lambda: False)
    called = {"n": 0}

    async def fake_super(self, *, run_id, tenant_id, project_id=None, run_service=None):
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
