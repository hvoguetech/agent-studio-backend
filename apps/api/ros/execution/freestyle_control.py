"""Freestyle run-control client — provision a VM per run and boot the ros runtime inside it.

Freestyle's SDK is Node, so — like the atlas builder — this calls a standalone `freestyle-svc`
HTTP control service (ROS_FREESTYLE_SERVICE_URL + secret) rather than driving Freestyle's VM
lifecycle from Python. `dispatch_run` asks the service to run `python -m ros.runtime drive --run-id
<id>` on a VM, injecting as env the master URL + a run token AND the shared runtime creds
(ROS_DATABASE_URL / ROS_REDIS_URL / ROS_SECRET_KEY / checkpointer) so the VM drives the run
against the SAME durable state the master uses (trusted-VM model).

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
    master_url: str, run_token: str, sticky_key: str | None = None,
    public: bool = False, run_context: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Ask freestyle-svc to boot the ros runtime for `run_id` on a VM. Returns a receipt
    ({vm_id, ...}); the VM drives the run and writes state to the shared Postgres. Raises on
    transport / non-2xx so the backend can fall back or surface the failure.

    When `sticky_key` is set (warm-VM mode), the service is asked to REUSE a warm VM for that key
    (the agent's workflow id) instead of cold-booting one per run - ⚠️ LIVE-VERIFY the svc honors
    `stickyKey`/`warm`; falling back to a fresh VM is a safe default if it doesn't."""
    # Trusted-VM model: the VM drives the run via `ros.runtime drive`, reading the run + workflow +
    # resolved secrets from the SHARED DB and streaming to the relay bus (creds injected as env at
    # provision time). master_url + the run token are still passed for the DB-less manifest-pull
    # fallback / future stricter isolation.
    command = f"python -m ros.runtime drive --run-id {run_id} --tenant {tenant_id}"
    if project_id:
        command += f" --project {project_id}"
    if public:
        command += " --public"  # embed surface -> the VM's _drive redacts operator-only frames (H5)
    env = {"ROS_MASTER_URL": master_url, "ROS_RUNTIME_TOKEN": run_token}
    # Trusted-VM model: the VM's `ros.runtime drive` reads the run + workflow + resolved secrets from
    # the SHARED durable state and streams to the relay bus, so it MUST point at the same Postgres +
    # Redis + master key + checkpointer the master uses. Without these the VM falls back to its baked
    # local SQLite (empty -> "no such table: runs") and the run never leaves `queued`. Injected here
    # (ROS_-prefixed so pydantic-settings picks them up) rather than baked into the snapshot, so creds
    # stay per-deploy and never live in the image.
    for var, val in (
        ("ROS_DATABASE_URL", settings.database_url),
        ("ROS_REDIS_URL", settings.redis_url),
        ("ROS_SECRET_KEY", settings.secret_key),
        ("ROS_CHECKPOINT_BACKEND", settings.checkpoint_backend),
        ("ROS_CHECKPOINT_POSTGRES_URL", settings.checkpoint_postgres_url),
        ("ROS_ENVIRONMENT", settings.environment),
    ):
        if val:
            env[var] = str(val)
    if run_context:
        import json
        # Per-run context (end-user / request scope) as JSON env, avoiding shell-quoting in `command`.
        env["ROS_RUN_CONTEXT"] = json.dumps(run_context, default=str)
    return await _post_run(
        run_id=run_id, tenant_id=tenant_id, project_id=project_id,
        command=command, env=env, sticky_key=sticky_key, client=client,
    )


async def dispatch_sandbox_run(
    *, run_id: str, tenant_id: str, project_id: str | None,
    master_url: str, run_token: str, run_input: dict | None = None,
    sticky_key: str | None = None, public: bool = False, run_context: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Dispatch a run to an ISOLATING sandbox (WS10 Phase 1). Unlike `dispatch_run`, the sandbox
    process gets ONLY `ROS_MASTER_URL` + the run-scoped token — NO shared DB/Redis/master-key. It
    pulls the manifest and streams/finalizes via master's runtime callbacks. That omission IS the
    isolation boundary (design/sandbox-backend-build-plan.md)."""
    command = (
        f"python -m ros.runtime sandbox --run-id {run_id} "
        f"--master-url {shlex_quote(master_url)} --token {shlex_quote(run_token)}"
    )
    if run_input:
        import json
        command += f" --input {shlex_quote(json.dumps(run_input, default=str))}"
    if public:
        command += " --public"
    # ONLY the master URL + token. Deliberately NO ROS_DATABASE_URL / ROS_REDIS_URL / ROS_SECRET_KEY.
    env = {"ROS_MASTER_URL": master_url, "ROS_RUNTIME_TOKEN": run_token}
    if run_context:
        import json
        env["ROS_RUN_CONTEXT"] = json.dumps(run_context, default=str)
    return await _post_run(
        run_id=run_id, tenant_id=tenant_id, project_id=project_id,
        command=command, env=env, sticky_key=sticky_key, client=client,
    )


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(str(s))


async def _post_run(
    *, run_id: str, tenant_id: str, project_id: str | None,
    command: str, env: dict[str, str], sticky_key: str | None,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """POST /run on freestyle-svc with a prebuilt command + env; returns the receipt."""
    body: dict[str, Any] = {
        "runId": run_id, "tenantId": tenant_id, "projectId": project_id,
        "command": command,
        "env": env,
    }
    if sticky_key:
        body["stickyKey"] = sticky_key
        body["warm"] = True
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
