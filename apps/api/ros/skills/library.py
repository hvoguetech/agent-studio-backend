"""Skill library -> a read-only filesystem the skills middleware can mount.

Two pieces:

- `materialize(skills)` renders library rows into `{path: text}` under `/skills/`, following the
  Agent Skills layout (`/skills/<name>/SKILL.md`, YAML frontmatter + markdown body). The
  frontmatter is SYNTHESIZED from the row's `name`/`description`, never authored, so a skill can
  never disagree with its own metadata and the name always matches its directory (a hard
  requirement of the spec — `deepagents`' parser drops any skill that violates it).
- `SkillLibraryBackend` serves that map to `SkillsMiddleware`. It subclasses `StateBackend`
  purely to override the ONE hook every read path funnels through (`_read_files`), which is why
  ls/read/grep/glob/download all work without reimplementation. Writes are refused: a skill is
  library content, and the agent editing it mid-run would be an invisible mutation of a
  tenant-scoped record.
"""

from __future__ import annotations

from typing import Any

from deepagents.backends.protocol import DeleteResult, EditResult, WriteResult
from deepagents.backends.state import StateBackend

# Where the library mounts. A source path, not a real directory — the backend is virtual.
SKILLS_ROOT = "/skills/"

MAX_SKILL_NAME_LENGTH = 64
_READ_ONLY = "the skill library is read-only; edit the skill in the Skills tab instead"


class InvalidSkillName(ValueError):
    """A skill name that the Agent Skills spec would reject (so the middleware would drop it)."""


def validate_skill_name(name: str) -> str:
    """Enforce the Agent Skills name rules, mirroring `deepagents`' own validator.

    We check at the API boundary rather than letting the middleware silently skip a bad skill at
    runtime — a skill that never loads is far harder to notice than a rejected save.
    """
    name = (name or "").strip()
    if not name:
        raise InvalidSkillName("name is required")
    if len(name) > MAX_SKILL_NAME_LENGTH:
        raise InvalidSkillName(f"name exceeds {MAX_SKILL_NAME_LENGTH} characters")
    if name.startswith("-") or name.endswith("-") or "--" in name:
        raise InvalidSkillName("name must be lowercase alphanumeric with single hyphens only")
    for c in name:
        if c == "-" or (c.isalpha() and c.islower()) or c.isdigit():
            continue
        raise InvalidSkillName("name must be lowercase alphanumeric with single hyphens only")
    return name


def _frontmatter(name: str, description: str) -> str:
    # Quote-and-escape the description: it is user text on a YAML scalar line, so a stray colon
    # or '#' would otherwise change the document's meaning (or fail to parse and drop the skill).
    desc = (description or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    return f'---\nname: {name}\ndescription: "{desc}"\n---\n'


def skill_files(skill: dict) -> dict[str, str]:
    """One skill row -> its files, keyed relative to the MOUNT ROOT (`/<name>/SKILL.md`).

    Paths are mount-relative, not `/skills/...`-prefixed, because `CompositeBackend` strips the
    route prefix before delegating and re-adds it to whatever the route returns. A backend that
    stored absolute `/skills/` paths would be invisible through the mount.
    """
    name = validate_skill_name(skill.get("name", ""))
    body = skill.get("content") or ""
    out = {f"/{name}/SKILL.md": _frontmatter(name, skill.get("description", "")) + body}
    for rel, text in (skill.get("files") or {}).items():
        # Supporting files are addressed by the SKILL.md body; keep them beside it, and never let
        # a relative path climb out of the skill's own directory.
        rel = str(rel).lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue
        out[f"/{name}/{rel}"] = text if isinstance(text, str) else str(text)
    return out


def mount(files: dict[str, str], default: Any) -> Any:
    """Layer a skill file map over `default` as a read-only `/skills/` route.

    The one supported way to attach the library: the route key, the source path the middleware
    is given, and the backend's own relative keys have to agree, and they only do here.
    """
    from deepagents.backends.composite import CompositeBackend

    return CompositeBackend(default=default, routes={SKILLS_ROOT: SkillLibraryBackend(files)})


def materialize(skills: list[dict]) -> dict[str, str]:
    """Library rows -> the full `{path: text}` map to mount (see `skill_files` for the keys).
    Later entries win on a name clash, matching the middleware's own source-layering semantics."""
    files: dict[str, str] = {}
    for skill in skills or []:
        try:
            files.update(skill_files(skill))
        except InvalidSkillName:
            continue  # a malformed row must not take the whole mount down
    return files


class SkillLibraryBackend(StateBackend):
    """A fixed, read-only file map. Everything inherited reads through `_read_files`."""

    def __init__(self, files: dict[str, str]) -> None:
        super().__init__()
        self._files: dict[str, dict[str, Any]] = {
            path: {"content": text} for path, text in (files or {}).items()
        }

    def _read_files(self) -> dict[str, Any]:
        return self._files

    # StateBackend's writes go through LangGraph channel sends; refuse instead of mutating state
    # that would never reach the library row anyway.
    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY)

    def edit(self, *args: Any, **kwargs: Any) -> EditResult:
        return EditResult(error=_READ_ONLY)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_READ_ONLY)
