"""Skill library loading — DB rows / manifest entries -> the `ctx.skill_library` index.

Indexed by id AND name so a workflow can reference a skill either way: ids survive a rename,
names survive an export/import into another project. The row dicts are what `ros.skills`
materializes into the mounted `/skills/` filesystem.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("ros.skills")


def to_row(skill) -> dict:
    """`Skill` model -> the plain dict carried on the context / in a RunManifest."""
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description or "",
        "content": skill.content or "",
        "files": skill.files or {},
    }


def index_skills(rows: list[dict]) -> dict[str, dict]:
    """Key each row by id and by name. Name entries never shadow an id entry (ids are exact)."""
    by_key: dict[str, dict] = {}
    for row in rows or []:
        if row.get("name"):
            by_key[row["name"]] = row
    for row in rows or []:
        if row.get("id"):
            by_key[row["id"]] = row
    return by_key


class SkillService:
    """Tenant/project-scoped CRUD over the `skills` table. Mirrors ComponentService."""

    @staticmethod
    async def list(session: AsyncSession, tenant_id: str, project_id: str) -> list:
        from ros.models import Skill

        rows = await session.execute(
            select(Skill).where(Skill.tenant_id == tenant_id, Skill.project_id == project_id)
        )
        return list(rows.scalars())

    @staticmethod
    async def get(session: AsyncSession, tenant_id: str, project_id: str, skill_id: str):
        # Scope by project as well as tenant so the project_id path segment is load-bearing.
        from ros.models import Skill

        row = await session.execute(
            select(Skill).where(
                Skill.tenant_id == tenant_id, Skill.project_id == project_id, Skill.id == skill_id
            )
        )
        return row.scalar_one_or_none()

    @staticmethod
    async def create(
        session: AsyncSession, tenant_id: str, project_id: str, *, name: str,
        description: str = "", content: str = "", files: dict | None = None,
    ):
        from ros.models import Skill

        skill = Skill(
            tenant_id=tenant_id, project_id=project_id, name=name,
            description=description or "", content=content or "", files=files or {},
        )
        session.add(skill)
        await session.commit()
        await session.refresh(skill)
        return skill

    @staticmethod
    async def update(
        session: AsyncSession, skill, *, name: str | None = None, description: str | None = None,
        content: str | None = None, files: dict | None = None, enabled: bool | None = None,
    ):
        changed = False
        for field, value in (("name", name), ("description", description), ("content", content),
                             ("files", files)):
            if value is not None and value != getattr(skill, field):
                setattr(skill, field, value)
                changed = True
        if enabled is not None:
            skill.enabled = enabled
        # Version tracks the MOUNTED content, so an enable/disable flip alone doesn't bump it.
        if changed:
            skill.version += 1
        await session.commit()
        await session.refresh(skill)
        return skill

    @staticmethod
    async def delete(session: AsyncSession, skill) -> None:
        """Agents reference skills by id in config["skills"]; the factory skips unknown ids, so
        deleting one degrades those agents rather than breaking their workflows."""
        await session.delete(skill)
        await session.commit()


async def load_skill_library(session: AsyncSession, tenant_id: str, project_id: str) -> dict[str, dict]:
    """The project's enabled skills, indexed for `ctx.skill_library`."""
    from ros.models import Skill

    try:
        rows = list((
            await session.execute(
                select(Skill).where(
                    Skill.tenant_id == tenant_id,
                    Skill.project_id == project_id,
                    Skill.enabled.is_(True),
                )
            )
        ).scalars())
    except Exception as e:  # noqa: BLE001 - a skills-table error must not abort the whole run
        log.warning("Skipping skills (load failed): %s", e)
        return {}
    return index_skills([to_row(s) for s in rows])
