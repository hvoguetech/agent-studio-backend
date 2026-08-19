"""Provisioning DX (#6 slice 1) — create / list / teardown an agent's isolated resource stack.

A thin surface over `ros.services.backend_provisioning` + the starter-template catalog. A template is
a named bundle of resources (db | db+storage | db+storage+queue); provisioning one loops
`provision_resource` per resource, scoped to (agent, optional end_user) — the forUser model — so the
resolved env is injected into that subject's runs (see #3). Best-effort (D3): a resource that fails
is reported, the rest still provision; each `provision_resource` rolls back its own external state.

Responses carry only resource handles (`secret://` refs + client-safe extras), never secret values.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ros.authz import require_permission
from ros.deps import CurrentUser, get_current_user, get_session, governed_subject_id
from ros.models import ProvisionedBackend
from ros.models.entities import ApiKey
from ros.services import backend_provisioning as bp
from ros.services import provision_templates as templates
from ros.services.apikeys import ApiKeyService
from ros.services.budget import ProvisionNotAllowed

router = APIRouter(prefix="/v1/projects/{project_id}/provisioning", tags=["provisioning"])

_PROVISION_CAP = "backend:provision"  # governed-subject capability an agent key needs to self-provision


class ProvisionIn(BaseModel):
    template: str | None = None          # a starter-template id (db | db+storage | db+storage+queue)
    kind: str | None = None              # OR a single provider kind (railway-postgres | ...)
    agent_id: str | None = None          # the governed subject; defaults to the caller's key id
    end_user_id: str | None = None       # forUser: scope this resource to one end user
    spec: dict | None = None             # provider-specific overrides (single-kind path)
    name: str | None = None


class ResourceOut(BaseModel):
    backend_id: str
    provider: str
    status: str
    endpoint_url: str | None = None
    agent_id: str | None = None
    end_user_id: str | None = None
    template: str | None = None


async def _resolve_agent_id(body: ProvisionIn, user: CurrentUser) -> str:
    """The governed subject to provision for. An API-key principal may only provision for its OWN
    key id (self-service); an operator (JWT) must name the agent_id explicitly."""
    kid = governed_subject_id(user)
    if kid:
        if body.agent_id and body.agent_id != kid:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "an API key may only provision for its own governed subject")
        return kid
    if not body.agent_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "agent_id is required")
    return body.agent_id


async def _gate_capability(session: AsyncSession, user: CurrentUser, tenant_id: str) -> None:
    """When the caller is an API key, enforce its default-deny capability allow-list + per-subject
    capacity cap. Operators (JWT/service) are already gated by the route's role permission."""
    kid = governed_subject_id(user)
    if not kid:
        return
    key = (await session.execute(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.id == kid)
    )).scalar_one_or_none()
    if key is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unknown API key principal")
    if not ApiKeyService.allows(key, _PROVISION_CAP):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"this API key lacks the '{_PROVISION_CAP}' capability")
    try:
        await ApiKeyService.enforce_capacity(session, key)
    except ProvisionNotAllowed as e:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(e)) from e


@router.get("/templates", dependencies=[Depends(require_permission("backend:read"))])
async def list_templates(project_id: str):
    """The starter-template catalog, each annotated with whether its providers are configured here."""
    return templates.list_templates()


@router.get("/resources", response_model=list[ResourceOut], dependencies=[Depends(require_permission("backend:read"))])
async def list_resources(
    project_id: str,
    agent_id: str | None = None,
    end_user_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """List the provisioned resources for this project (optionally filtered by agent / end user).
    Never returns secret values — only the row metadata."""
    where = [ProvisionedBackend.tenant_id == user.tenant_id, ProvisionedBackend.project_id == project_id,
             ProvisionedBackend.status != "deleted"]
    if agent_id:
        where.append(ProvisionedBackend.agent_id == agent_id)
    if end_user_id:
        where.append(ProvisionedBackend.end_user_id == end_user_id)
    rows = (await session.execute(select(ProvisionedBackend).where(*where).order_by(ProvisionedBackend.created_at.desc()))).scalars()
    return [
        ResourceOut(
            backend_id=r.id, provider=r.provider, status=r.status, endpoint_url=r.endpoint_url,
            agent_id=r.agent_id, end_user_id=r.end_user_id, template=(r.config or {}).get("template"),
        )
        for r in rows
    ]


@router.post("/provision", dependencies=[Depends(require_permission("backend:provision"))])
async def provision(
    project_id: str,
    body: ProvisionIn,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Provision a template (a stack) or a single resource kind for a governed subject, optionally
    scoped to one end user. Best-effort: returns each resource's outcome (`provisioned` + `errors`)."""
    tenant_id = user.tenant_id
    agent_id = await _resolve_agent_id(body, user)
    await _gate_capability(session, user, tenant_id)

    # Resolve the resource list: a template (its canonical per-resource kinds) OR a single kind.
    if body.template:
        resources = templates.resources_for(body.template)
        if resources is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown template {body.template!r}")
        template_id: str | None = body.template
    elif body.kind:
        resources = [{"kind": body.kind, "spec": body.spec or {}}]
        template_id = None
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide either `template` or `kind`")

    result = await bp.provision_resource_list(
        session, tenant_id, project_id, agent_id=agent_id, end_user_id=body.end_user_id,
        resources=resources, template_id=template_id, name=body.name,
    )
    provisioned, errors = result["provisioned"], result["errors"]

    # A request that provisioned nothing is a failure, not a silent empty success.
    if not provisioned and errors:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"message": "provisioning failed", "errors": errors})

    return {"template": template_id, "agent_id": agent_id, "end_user_id": body.end_user_id,
            "provisioned": provisioned, "errors": errors}


@router.delete("/resources/{backend_id}", dependencies=[Depends(require_permission("backend:provision"))])
async def teardown(
    project_id: str,
    backend_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Tear down a provisioned resource: destroys the external resource, its secrets, and the row."""
    try:
        return await bp.teardown_resource(session, user.tenant_id, project_id, backend_id=backend_id)
    except bp.ProvisionError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
