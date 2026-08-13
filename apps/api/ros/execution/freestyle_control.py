"""Freestyle run-control client — provision a VM per run and boot the ros runtime inside it.

Freestyle's SDK is Node, so — like the atlas builder — this calls a standalone `freestyle-svc`
HTTP control service (ROS_FREESTYLE_SERVICE_URL + secret) rather than driving Freestyle's VM
lifecycle from Python. `dispatch_run` asks the service to run `python -m ros.runtime run --run-id
<id>` on a VM, with the master URL + a run token injected as env so the runtime can pull the run
manifest and drive it against the shared durable state (trusted-VM model).

⚠️ LIVE-VERIFY: the freestyle-svc `/run` endpoint shape must be confirmed against the deployed
service on first use. Disabled (no-op) when ROS_FREESTYLE_SERVICE_URL is unset.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ros.config import settings

logger = logging.getLogger("ros.execution.freestyle")

_TIMEOUT = 60.0


def is_enabled() -> bool:
    return bool(settings.freestyle_service_url)


def _client() -> httpx.AsyncClient:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.freestyle_service_secret:
        headers["Authorization"] = f"Bearer {settings.freestyle_service_secret}"
    return httpx.AsyncClient(base_url=settings.freestyle_service_url.rstrip("/"), timeout=_TIMEOUT, headers=headers)


async def dispatch_run(
    *, run_id: str, tenant_id: str, project_id: str | None,
    master_url: str, run_token: str, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Ask freestyle-svc to boot the ros runtime for `run_id` on a VM. Returns a receipt
    ({vm_id, ...}); the VM drives the run and writes state to the shared Postgres. Raises on
    transport / non-2xx so the backend can fall back or surface the failure."""
    # Trusted-VM model: the VM drives the run via `ros.runtime drive`, reading the run + workflow +
    # resolved secrets from the SHARED DB and streaming to the relay bus (creds injected as env at
    # provision time). master_url + the run token are still passed for the DB-less manifest-pull
    # fallback / future stricter isolation.
    command = f"python -m ros.runtime drive --run-id {run_id} --tenant {tenant_id}"
    if project_id:
        command += f" --project {project_id}"
    body = {
        "runId": run_id, "tenantId": tenant_id, "projectId": project_id,
        "command": command,
        "env": {"ROS_MASTER_URL": master_url, "ROS_RUNTIME_TOKEN": run_token},
    }
    own = client is None
    c = client or _client()
    try:
        resp = await c.post("/run", json=body)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(f"freestyle-svc /run -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}
    finally:
        if own:
            await c.aclose()
