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
from ros.config import settings
from ros.deps import CurrentUser, current_tenant_id, get_session, require_role
from ros.services.skills import SkillService
from ros.skills import (
    InvalidSkillName,
    SkillImportError,
    dedupe_name,
    parse_skill_tree,
    validate_skill_name,
)

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


class SkillImportIn(BaseModel):
    """A skills TREE, keyed by path relative to the folder that was picked — exactly what a
    `.claude/skills/` directory looks like. Text only: the library stores text, and a skill's
    supporting files are docs/templates, not binaries."""

    files: dict[str, str]


class SkillImportReport(BaseModel):
    imported: int
    skipped: int = 0
    # {name, id, source_path, renamed_from?, dropped_frontmatter?} per import; {path, reason} per skip.
    items: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@router.post("/import", response_model=SkillImportReport,
             dependencies=[Depends(require_permission("skill:write"))])
async def import_skills(project_id: str, body: SkillImportIn,
                        session: AsyncSession = Depends(get_session),
                        tenant_id: str = Depends(current_tenant_id),
                        _: CurrentUser = Depends(require_role("editor"))):
    """Import Claude-Code-style skill folders. Same spec, so this parses rather than converts.

    Collisions RENAME rather than overwrite — an import must never silently replace a skill some
    agent is already attached to."""
    cap = settings.artifact_max_bytes or 0
    total = sum(len(v) for v in body.files.values())
    if cap and total > cap:
        raise HTTPException(413, f"skill upload is {total} bytes; exceeds the {cap}-byte limit")
    try:
        parsed, skipped = parse_skill_tree(body.files)
    except SkillImportError as e:
        raise HTTPException(422, str(e)) from e

    taken = {s.name for s in await SkillService.list(session, tenant_id, project_id)}
    items: list[dict] = [{"path": s["path"], "skipped": s["reason"]} for s in skipped]
    warnings: list[str] = []
    imported = 0
    for skill in parsed:
        original = skill["name"]
        name = dedupe_name(original, taken)
        taken.add(name)
        row = await SkillService.create(
            session, tenant_id, project_id, name=name, description=skill["description"],
            content=skill["content"], files=skill["files"],
        )
        imported += 1
        item = {"id": row.id, "name": name, "source_path": skill["source_path"]}
        if name != original:
            item["renamed_from"] = original
            warnings.append(f"'{original}' already existed — imported as '{name}'.")
        if skill["dropped_frontmatter"]:
            item["dropped_frontmatter"] = skill["dropped_frontmatter"]
            # Say it out loud: the library re-synthesizes frontmatter from its own columns, so
            # these keys are gone, not merely unused.
            warnings.append(
                f"'{name}': dropped frontmatter {', '.join(skill['dropped_frontmatter'])} "
                "— the library only keeps name and description."
            )
        if not skill["description"]:
            warnings.append(f"'{name}' has no description, so the agent has nothing to match on.")
        items.append(item)
    return SkillImportReport(imported=imported, skipped=len(skipped), items=items, warnings=warnings)


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
