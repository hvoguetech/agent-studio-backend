"""Agent skills — the library rows in `skills` made mountable by a deep_agent node.

`materialize` turns Skill records into the `/skills/<name>/SKILL.md` file map the Agent Skills
spec expects; `SkillLibraryBackend` serves that map read-only to `SkillsMiddleware`. See
docs/specs/artifacts-and-code-node.md §4.4.
"""

from ros.skills.importer import (
    SKILL_FILE,
    SkillImportError,
    dedupe_name,
    parse_skill_tree,
    split_frontmatter,
)
from ros.skills.library import (
    SKILLS_ROOT,
    InvalidSkillName,
    SkillLibraryBackend,
    materialize,
    mount,
    skill_files,
    validate_skill_name,
)

__all__ = [
    "SKILLS_ROOT",
    "SKILL_FILE",
    "InvalidSkillName",
    "SkillImportError",
    "SkillLibraryBackend",
    "dedupe_name",
    "materialize",
    "mount",
    "parse_skill_tree",
    "skill_files",
    "split_frontmatter",
    "validate_skill_name",
]
