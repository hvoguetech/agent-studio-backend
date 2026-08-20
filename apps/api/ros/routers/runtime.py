"""Runtime endpoints — what a standalone runtime (a sandbox / Freestyle VM) uses to run a workflow
WITHOUT the master DB, authenticated ONLY by the run-scoped token minted at dispatch (scope
`runtime:pull`, bound to run_id + tenant, expiring, revocable).

Two halves of the control-plane / data-plane split (design/sandbox-backend-build-plan.md):

- READ  `GET  /v1/runtime/runs/{run_id}/manifest` — pull the RunManifest (workflow + defs + RESOLVED
        run-scoped secrets) to rebuild a CompileContext offline.
- WRITE `POST /v1/runtime/runs/{run_id}/frames`   — append SSE frames (relayed to the browser).
        `POST /v1/runtime/runs/{run_id}/status`   — running/heartbeat lease stamp.
        `POST /v1/runtime/runs/{run_id}/result`   — terminal status + answer/tokens/cost/spans.

The isolating `sandbox` backend holds NO DB/Redis/master-key creds — it reads and writes everything
through here, tenant-scoped SERVER-SIDE (tenant is taken from the token, never from the body; RLS via
a sandbox-set GUC is not a boundary). Marked public_endpoint so the app-wide default-deny guard defers
to the run-token check here.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ros.authz import public_endpoint
from ros.db.scoping import set_current_tenant
from ros.deps import get_session
from ros.models import Run, Thread
from ros.security import TokenError, decode_token
from ros.services.run_relay import publish_frame
from ros.services.runtime_manifest import RuntimeManifestService

router = APIRouter(prefix="/v1/runtime", tags=["runtime"])

# Callback scopes a run token may carry. `runtime:pull` (the only scope minted today) authorizes both
# the manifest read and the write-back callbacks — the sandbox holds exactly ONE credential.
_CALLBACK_SCOPES = {"runtime:pull", "runtime:drive"}
_TERMINAL_STATES = {"done", "error", "canceled", "interrupted"}


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def _authorize_run(token: str, run_id: str) -> str:
    """Verify a run token authorizes acting on `run_id`; return its tenant id. The tenant ALWAYS comes
    from the signed token, never from the request body (a sandbox-supplied tenant is not trusted)."""
    try:
        claims = decode_token(token, expected_type="run")
    except TokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid run token: {e}") from e
    if claims.get("sub") != run_id or claims.get("scope") not in _CALLBACK_SCOPES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "run token does not authorize this run")
    tenant_id = claims.get("tid")
    if not tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "run token missing tenant")
    return tenant_id


@router.get("/runs/{run_id}/manifest", dependencies=[Depends(public_endpoint)])
async def get_run_manifest(
    run_id: str, request: Request, session: AsyncSession = Depends(get_session)
):
    tenant_id = _authorize_run(_bearer(request), run_id)
    set_current_tenant(tenant_id)  # bind RLS for the reads below

    run = (await session.execute(
        select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    # The run's bound end user (from its thread) scopes the provisioned per-end-user resource env in
    # the manifest (2b); the governed subject is Run.agent_id (NULL for operator runs -> no env).
    thread = (await session.execute(
        select(Thread).where(Thread.id == run.thread_id, Thread.tenant_id == tenant_id)
    )).scalar_one_or_none()
    eu_id = str((((thread.meta if thread else None) or {}).get("end_user") or {}).get("id") or "") or None
    try:
        return await RuntimeManifestService.build(
            session, tenant_id=tenant_id, project_id=run.project_id, workflow_id=run.workflow_id,
            agent_id=run.agent_id, end_user_id=eu_id,
        )
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


# --- Write-back callbacks (the data-plane -> control-plane half) --------------------------------
# The sandbox posts here instead of touching the DB/Redis. Tenant is from the token (never the body).


class FramesIn(BaseModel):
    # Ordered SSE frames the sandbox produced, each {"seq": int, "event": str, "data": dict}. The
    # sandbox owns the monotonic seq (its broker), so relay replay/ordering matches a local run.
    frames: list[dict]


class StatusIn(BaseModel):
    status: str = "running"  # only the running/heartbeat transition; terminal goes via /result
    heartbeat: bool = True


class ResultIn(BaseModel):
    status: str                       # done | error | interrupted
    output: dict | None = None        # final graph state (already JSON-safe)
    error: str | None = None
    total_tokens: int | None = None
    total_cost_usd: float | None = None


async def _load_run(session: AsyncSession, run_id: str, tenant_id: str) -> Run:
    run = (await session.execute(
        select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    return run


@router.post("/runs/{run_id}/frames", dependencies=[Depends(public_endpoint)])
async def post_run_frames(run_id: str, body: FramesIn, request: Request):
    """Relay the sandbox's SSE frames to the shared bus so master's SSE endpoint streams them to the
    browser (live node progress). No DB write — pure fan-out."""
    tenant_id = _authorize_run(_bearer(request), run_id)
    for fr in body.frames:
        seq = int(fr.get("seq") or 0)
        frame = {"event": fr.get("event"), "data": fr.get("data")}
        await publish_frame(run_id, seq, frame, tenant_id=tenant_id)
    return {"ok": True, "count": len(body.frames)}


@router.post("/runs/{run_id}/status", dependencies=[Depends(public_endpoint)])
async def post_run_status(run_id: str, body: StatusIn, request: Request,
                          session: AsyncSession = Depends(get_session)):
    """Stamp running + heartbeat so the reaper / stuck-run watchdog see a live driver. Terminal
    transitions are NOT accepted here — they go through /result."""
    tenant_id = _authorize_run(_bearer(request), run_id)
    set_current_tenant(tenant_id)
    if body.status in _TERMINAL_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "use /result for terminal status")
    values: dict = {"status": body.status}
    if body.heartbeat:
        values["heartbeat_at"] = datetime.utcnow()
    # Targeted UPDATE so it can't lose-update a concurrent executor/vm_id write.
    await session.execute(
        update(Run).where(Run.id == run_id, Run.tenant_id == tenant_id).values(**values)
    )
    await session.commit()
    return {"ok": True}


@router.post("/runs/{run_id}/result", dependencies=[Depends(public_endpoint)])
async def post_run_result(run_id: str, body: ResultIn, request: Request,
                          session: AsyncSession = Depends(get_session)):
    """Persist the terminal state the sandbox drove to. One-shot: refuses to overwrite a run that is
    already terminal (idempotent-ish against a retried callback)."""
    tenant_id = _authorize_run(_bearer(request), run_id)
    if body.status not in {"done", "error", "interrupted"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid terminal status {body.status!r}")
    set_current_tenant(tenant_id)
    run = await _load_run(session, run_id, tenant_id)
    if run.status in _TERMINAL_STATES:
        return {"ok": True, "already": run.status}  # a retried callback is a no-op
    run.status = body.status
    if body.output is not None:
        run.output = body.output
    if body.error is not None:
        run.error = body.error
    if body.total_tokens is not None:
        run.total_tokens = body.total_tokens
    if body.total_cost_usd is not None:
        run.total_cost_usd = body.total_cost_usd
    run.ended_at = datetime.utcnow()
    await session.commit()
    return {"ok": True, "status": run.status}
