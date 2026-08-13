"""Runtime endpoint — the RunManifest a standalone runtime (a Freestyle VM) pulls to run a workflow.

`GET /v1/runtime/runs/{run_id}/manifest`, authenticated by the run-scoped token minted at dispatch
(scope `runtime:pull`, bound to run_id + tenant). The token carries the tenant; the run row gives the
workflow. The manifest includes RESOLVED secrets, so only the run's own VM (holding the token) can
fetch it — it's not a role-gated operator endpoint. Marked public_endpoint so the app-wide
default-deny guard defers to the run-token check here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ros.authz import public_endpoint
from ros.db.scoping import set_current_tenant
from ros.deps import get_session
from ros.models import Run
from ros.security import TokenError, decode_token
from ros.services.runtime_manifest import RuntimeManifestService

router = APIRouter(prefix="/v1/runtime", tags=["runtime"])


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


@router.get("/runs/{run_id}/manifest", dependencies=[Depends(public_endpoint)])
async def get_run_manifest(
    run_id: str, request: Request, session: AsyncSession = Depends(get_session)
):
    try:
        claims = decode_token(_bearer(request), expected_type="run")
    except TokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid run token: {e}") from e
    if claims.get("sub") != run_id or claims.get("scope") != "runtime:pull":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "run token does not authorize this run")
    tenant_id = claims.get("tid")
    if not tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "run token missing tenant")
    set_current_tenant(tenant_id)  # bind RLS for the reads below

    run = (await session.execute(
        select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    try:
        return await RuntimeManifestService.build(
            session, tenant_id=tenant_id, project_id=run.project_id, workflow_id=run.workflow_id
        )
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
