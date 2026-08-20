"""Import Claude-Code-style skill folders into the library.

ROS skills and Claude Code skills are the SAME artifact — the Anthropic Agent Skills spec: a
directory holding `SKILL.md` with `name`/`description` YAML frontmatter, plus supporting files.
So importing is parsing, not converting: drop a `.claude/skills/` tree in and each skill folder
becomes a row.

What does NOT survive: frontmatter keys beyond name/description (`allowed-tools`, `license`,
`metadata`, …). The library re-synthesizes frontmatter from its own columns, so anything else
would be silently dropped on the next save — the importer reports those keys instead of
pretending they were kept.
"""

from __future__ import annotations

import logging
import posixpath

import yaml

from ros.skills.library import InvalidSkillName, validate_skill_name

log = logging.getLogger("ros.skills")

SKILL_FILE = "SKILL.md"
# Only name/description shape the mounted skill; the rest is reported as dropped.
_KEPT_KEYS = {"name", "description"}


class SkillImportError(ValueError):
    """The uploaded tree contained nothing importable."""


def split_frontmatter(text: str) -> tuple[dict, str]:
    """`SKILL.md` -> (frontmatter mapping, body). No frontmatter is not an error here: the
    directory name can still supply the skill name, which is friendlier than rejecting a file
    that a human would call a skill."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n", 1)
    if len(parts) < 2:
        return {}, text
    rest = parts[1]
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    raw, body = rest[:end], rest[end + 4:]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        log.warning("skill frontmatter is not valid YAML: %s", e)
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, body.lstrip("\n")


def _dir_of(path: str) -> str:
    return posixpath.dirname(path.strip("/"))


def parse_skill_tree(files: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """`{relative path: text}` -> (parsed skills, skipped entries).

    A skill is any directory containing `SKILL.md`; every other file under that directory rides
    along as a supporting file, keyed relative to the skill folder. Files that belong to no skill
    directory are reported rather than dropped, so an upload of the wrong folder says so instead
    of silently importing nothing.
    """
    skill_dirs = {_dir_of(p): p for p in files if posixpath.basename(p.strip("/")) == SKILL_FILE}
    if not skill_dirs:
        raise SkillImportError(
            f"no {SKILL_FILE} found — pick a skills folder (each skill is a directory "
            f"containing {SKILL_FILE})"
        )

    parsed: list[dict] = []
    skipped: list[dict] = []
    claimed: set[str] = set()

    for directory, md_path in sorted(skill_dirs.items()):
        meta, body = split_frontmatter(files[md_path])
        dir_name = posixpath.basename(directory) if directory else ""
        # The spec requires frontmatter name == directory name; when they disagree (or the name
        # is missing/unusable) fall back to the directory, which is what the mount will use.
        candidates = [str(meta.get("name") or "").strip(), dir_name]
        name = None
        for candidate in candidates:
            try:
                name = validate_skill_name(candidate)
                break
            except InvalidSkillName:
                continue
        if name is None:
            skipped.append({"path": md_path, "reason": "no usable skill name (frontmatter or folder)"})
            continue

        supporting: dict[str, str] = {}
        prefix = f"{directory}/" if directory else ""
        for path, text in files.items():
            if path == md_path or not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue
            supporting[rel] = text
            claimed.add(path)
        claimed.add(md_path)

        dropped = sorted(k for k in meta if k not in _KEPT_KEYS)
        parsed.append({
            "name": name,
            "description": str(meta.get("description") or "").strip(),
            "content": body,
            "files": supporting,
            "source_path": md_path,
            "dropped_frontmatter": dropped,
        })

    for path in sorted(files):
        if path not in claimed:
            skipped.append({"path": path, "reason": f"not inside a folder containing {SKILL_FILE}"})
    return parsed, skipped


def dedupe_name(name: str, taken: set[str]) -> str:
    """A free name in `taken`'s namespace. Mirrors the portability importer: rename rather than
    overwrite, so an import can never quietly replace a skill someone else is using."""
    if name not in taken:
        return name
    for i in range(2, 1000):
        candidate = f"{name}-{i}"
        if candidate not in taken and len(candidate) <= 64:
            return candidate
    raise SkillImportError(f"cannot find a free name for {name!r}")
