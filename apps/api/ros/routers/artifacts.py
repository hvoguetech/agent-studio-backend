"""Artifact endpoints (WS7 phase 2) - upload/list/download/delete of agent/tool-produced files.

Bytes live in the object store (ros.artifacts); this router records/serves the tenant/project-scoped
`Artifact` rows and issues authorized downloads (presigned URL for s3, streamed for local). Gated by
`artifact:read` (list/download) and `artifact:write` (upload/delete)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ros.artifacts import ArtifactRef, ObjectStoreError, get_artifact_store
from ros.authz import require_permission
from ros.deps import current_tenant_id, get_session
from ros.models import Artifact

router = APIRouter(prefix="/v1/projects/{project_id}/artifacts", tags=["artifacts"])


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    run_id: str | None = None
    filename: str | None = None
    content_type: str
    size: int
    sha256: str


def _ref(art: Artifact) -> ArtifactRef:
    return ArtifactRef(
        bucket=art.bucket, key=art.key, sha256=art.sha256, size=art.size,
        content_type=art.content_type, filename=art.filename,
    )


async def _get(session: AsyncSession, tenant_id: str, project_id: str, artifact_id: str) -> Artifact:
    art = (
        await session.execute(
            select(Artifact).where(
                Artifact.id == artifact_id,
                Artifact.tenant_id == tenant_id,
                Artifact.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    return art


@router.post("", response_model=ArtifactOut, status_code=201,
             dependencies=[Depends(require_permission("artifact:write"))])
async def upload_artifact(
    project_id: str,
    file: UploadFile = File(...),
    run_id: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(current_tenant_id),
):
    data = await file.read()
    try:
        ref = await get_artifact_store().put(
            tenant_id=tenant_id, project_id=project_id, run_id=run_id or "adhoc",
            data=data, filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
        )
    except ObjectStoreError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    art = Artifact(
        tenant_id=tenant_id, project_id=project_id, run_id=run_id, bucket=ref.bucket, key=ref.key,
        sha256=ref.sha256, size=ref.size, content_type=ref.content_type, filename=ref.filename,
    )
    session.add(art)
    await session.commit()
    await session.refresh(art)
    return art


@router.get("", response_model=list[ArtifactOut],
            dependencies=[Depends(require_permission("artifact:read"))])
async def list_artifacts(
    project_id: str,
    run_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(current_tenant_id),
):
    stmt = select(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.project_id == project_id)
    if run_id:
        stmt = stmt.where(Artifact.run_id == run_id)
    rows = (await session.execute(stmt.order_by(Artifact.created_at.desc()))).scalars().all()
    return list(rows)


@router.get("/{artifact_id}/download", dependencies=[Depends(require_permission("artifact:read"))])
async def download_artifact(
    project_id: str,
    artifact_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(current_tenant_id),
):
    art = await _get(session, tenant_id, project_id, artifact_id)
    store = get_artifact_store()
    ref = _ref(art)
    url = await store.presign(ref, expires_s=900)
    if url.startswith("http"):  # s3 -> hand back a short-lived presigned URL
        return RedirectResponse(url)
    # local backend -> stream the bytes as an attachment (never inline; untrusted content).
    data = await store.get(ref)
    return Response(
        content=data,
        media_type=art.content_type,
        headers={"Content-Disposition": f'attachment; filename="{art.filename or "artifact"}"'},
    )


@router.delete("/{artifact_id}", status_code=204,
               dependencies=[Depends(require_permission("artifact:write"))])
async def delete_artifact(
    project_id: str,
    artifact_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(current_tenant_id),
):
    art = await _get(session, tenant_id, project_id, artifact_id)
    try:
        await get_artifact_store().delete(_ref(art))
    except ObjectStoreError:
        pass  # object best-effort; the row delete below makes it unreachable regardless
    await session.execute(sa_delete(Artifact).where(Artifact.id == art.id))
    await session.commit()
