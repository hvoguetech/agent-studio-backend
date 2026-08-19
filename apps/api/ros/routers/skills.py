"""Skill library endpoints (CRUD) — the agent skills a deep_agent node can attach.

A Skill is a name + description + SKILL.md body (plus optional supporting files). It is attached
to an agent like a component is (agent `config["skills"]` holds skill ids); at runtime the
referenced skills are mounted read-only at `/skills/<name>/` and surfaced to the model by
description, with the body read on demand. Gated by `skill:read` / `skill:write`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ros.authz import require_permission
from ros.deps import CurrentUser, current_tenant_id, get_session, require_role
from ros.services.skills import SkillService
from ros.skills import InvalidSkillName, validate_skill_name

router = APIRouter(prefix="/v1/projects/{project_id}/skills", tags=["skills"])

# The Agent Skills spec's name rules. Enforced here as well as in validate_skill_name so a bad
# name is a 422 at the boundary rather than a skill that silently never loads at runtime.
_NAME = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class SkillCreate(BaseModel):
    name: str = Field(pattern=_NAME, max_length=64)
    description: str = ""
    content: str = ""
    files: dict[str, str] = Field(default_factory=dict)


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, pattern=_NAME, max_length=64)
    description: str | None = None
    content: str | None = None
    files: dict[str, str] | None = None
    enabled: bool | None = None


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str = ""
    content: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    version: int = 1


def _check_name(name: str) -> None:
    try:
        validate_skill_name(name)
    except InvalidSkillName as e:
        raise HTTPException(422, str(e)) from e


@router.get("", response_model=list[SkillOut], dependencies=[Depends(require_permission("skill:read"))])
async def list_skills(project_id: str, session: AsyncSession = Depends(get_session),
                      tenant_id: str = Depends(current_tenant_id)):
    return await SkillService.list(session, tenant_id, project_id)


@router.post("", response_model=SkillOut, status_code=201,
             dependencies=[Depends(require_permission("skill:write"))])
async def create_skill(project_id: str, body: SkillCreate, session: AsyncSession = Depends(get_session),
                       tenant_id: str = Depends(current_tenant_id),
                       _: CurrentUser = Depends(require_role("editor"))):
    _check_name(body.name)
    existing = await SkillService.list(session, tenant_id, project_id)
    # The name is the mounted directory, so a duplicate would shadow rather than coexist.
    if any(s.name == body.name for s in existing):
        raise HTTPException(409, f"A skill named '{body.name}' already exists in this project.")
    return await SkillService.create(session, tenant_id, project_id, **body.model_dump())


@router.get("/{skill_id}", response_model=SkillOut, dependencies=[Depends(require_permission("skill:read"))])
async def get_skill(project_id: str, skill_id: str, session: AsyncSession = Depends(get_session),
                    tenant_id: str = Depends(current_tenant_id)):
    skill = await SkillService.get(session, tenant_id, project_id, skill_id)
    if skill is None:
        raise HTTPException(404, "Skill not found")
    return skill


@router.patch("/{skill_id}", response_model=SkillOut,
              dependencies=[Depends(require_permission("skill:write"))])
async def update_skill(project_id: str, skill_id: str, body: SkillUpdate,
                       session: AsyncSession = Depends(get_session),
                       tenant_id: str = Depends(current_tenant_id),
                       _: CurrentUser = Depends(require_role("editor"))):
    skill = await SkillService.get(session, tenant_id, project_id, skill_id)
    if skill is None:
        raise HTTPException(404, "Skill not found")
    if body.name and body.name != skill.name:
        _check_name(body.name)
        existing = await SkillService.list(session, tenant_id, project_id)
        if any(s.name == body.name and s.id != skill.id for s in existing):
            raise HTTPException(409, f"A skill named '{body.name}' already exists in this project.")
    return await SkillService.update(session, skill, **body.model_dump(exclude_unset=True))


@router.delete("/{skill_id}", status_code=204,
               dependencies=[Depends(require_permission("skill:write"))])
async def delete_skill(project_id: str, skill_id: str, session: AsyncSession = Depends(get_session),
                       tenant_id: str = Depends(current_tenant_id),
                       _: CurrentUser = Depends(require_role("editor"))):
    skill = await SkillService.get(session, tenant_id, project_id, skill_id)
    if skill is None:
        raise HTTPException(404, "Skill not found")
    await SkillService.delete(session, skill)
