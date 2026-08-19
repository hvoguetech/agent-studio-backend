"""Agent skills: library rows -> a mounted read-only /skills/ filesystem a deep_agent discovers.

Covers the three places this can silently fail: materialization (a skill whose frontmatter doesn't
match its directory is dropped by `deepagents` at runtime), the compile wiring (skills without a
read_file tool are undiscoverable), and the read-only guarantee.
"""

from __future__ import annotations

import pytest
from deepagents.middleware.skills import _list_skills_with_errors

from ros.engine.context import CompileContext
from ros.nodes.agent_node import _resolve_skills, agent_factory
from ros.services.skills import index_skills
from ros.skills import (
    SKILLS_ROOT,
    InvalidSkillName,
    SkillLibraryBackend,
    materialize,
    mount,
    validate_skill_name,
)

RESEARCH = {
    "id": "sk-1", "name": "web-research",
    "description": "Structured approach to conducting thorough web research",
    "content": "# Web research\n\n1. Search broadly\n2. Read primary sources\n",
    "files": {"checklist.md": "- sources cited"},
}


def _ctx(**kw) -> CompileContext:
    return CompileContext(tenant_id="t", project_id="p", default_model="fake", **kw)


# --- name validation (mirrors the Agent Skills spec) ---
@pytest.mark.parametrize("name", ["web-research", "a", "skill-1", "a-b-c"])
def test_valid_names(name):
    assert validate_skill_name(name) == name


@pytest.mark.parametrize("name", ["", "Web-Research", "web_research", "-lead", "trail-", "a--b", "x" * 65])
def test_invalid_names(name):
    with pytest.raises(InvalidSkillName):
        validate_skill_name(name)


# --- materialization ---
def test_materialize_lays_out_the_spec_structure():
    files = materialize([RESEARCH])
    assert set(files) == {"/web-research/SKILL.md", "/web-research/checklist.md"}
    md = files["/web-research/SKILL.md"]
    assert md.startswith("---\nname: web-research\n")
    assert md.endswith(RESEARCH["content"])


def test_paths_are_mount_relative_not_skills_prefixed():
    """CompositeBackend strips the route prefix before delegating and re-adds it on the way out,
    so a backend keyed on absolute /skills/ paths is invisible through the mount. Pin the shape."""
    assert all(not p.startswith(SKILLS_ROOT) for p in materialize([RESEARCH]))


def test_frontmatter_survives_a_description_with_yaml_syntax():
    """An unquoted colon or quote in a description would break the YAML and silently drop the
    skill, so the frontmatter is escaped rather than interpolated raw."""
    files = materialize([{"name": "x", "description": 'Handles: "quotes", colons # and hashes',
                          "content": "body"}])
    skills, err = _list_skills_with_errors(_mounted(files), SKILLS_ROOT)
    assert err is None
    assert skills[0]["description"] == 'Handles: "quotes", colons # and hashes'


def test_malformed_skill_is_skipped_not_fatal():
    files = materialize([{"name": "BAD NAME", "content": "x"}, RESEARCH])
    assert all("BAD NAME" not in p for p in files)
    assert "/web-research/SKILL.md" in files


def test_supporting_file_cannot_escape_its_skill_directory():
    files = materialize([{**RESEARCH, "files": {"../../etc/passwd": "x", "/abs.md": "y"}}])
    assert all(".." not in p for p in files)
    assert "/web-research/abs.md" in files  # leading slash stripped, kept in-dir


# --- what deepagents actually sees, through the real mount ---
def _mounted(files: dict):
    from deepagents.backends.state import StateBackend

    return mount(files, StateBackend())


def test_deepagents_discovers_a_mounted_skill():
    skills, err = _list_skills_with_errors(_mounted(materialize([RESEARCH])), SKILLS_ROOT)
    assert err is None
    assert [s["name"] for s in skills] == ["web-research"]
    assert skills[0]["description"] == RESEARCH["description"]
    # The path the model is SHOWN must be the path it can read_file — the whole contract.
    assert skills[0]["path"] == f"{SKILLS_ROOT}web-research/SKILL.md"


def test_mounted_skill_is_readable_at_the_advertised_path():
    backend = _mounted(materialize([RESEARCH]))
    read = backend.read(f"{SKILLS_ROOT}web-research/SKILL.md")
    assert read.file_data["content"].endswith(RESEARCH["content"])


def test_mount_is_read_only():
    backend = _mounted(materialize([RESEARCH]))
    path = f"{SKILLS_ROOT}web-research/SKILL.md"
    assert backend.write(path, "hacked").error
    assert backend.delete(path).error
    assert backend.read(path).file_data["content"].endswith(RESEARCH["content"])  # unchanged


def test_mount_layers_over_the_existing_backend():
    """Attaching a skill must not shadow the agent's own filesystem — /skills/ is a route on top
    of whatever backend the node already had."""
    node_backend = SkillLibraryBackend({"/notes.txt": "mine"})  # stands in for the node's own FS
    backend = mount(materialize([RESEARCH]), node_backend)
    assert backend.read("/notes.txt").file_data["content"] == "mine"
    skill = backend.read(f"{SKILLS_ROOT}web-research/SKILL.md")
    assert skill.file_data["content"].endswith(RESEARCH["content"])


# --- resolution against the library ---
def test_resolve_by_id_and_by_name():
    ctx = _ctx(skill_library=index_skills([RESEARCH]))
    for ref in ("sk-1", "web-research"):
        files, sources = _resolve_skills([ref], ctx)
        assert "/web-research/SKILL.md" in files
        assert sources == []


def test_unknown_skill_is_skipped_like_an_unknown_tool_id():
    files, sources = _resolve_skills(["nope"], _ctx(skill_library=index_skills([RESEARCH])))
    assert files == {} and sources == []


def test_absolute_entry_passes_through_as_a_source_path():
    """How the console assistant mounts its own on-disk skills — kept working."""
    files, sources = _resolve_skills(["/skills/"], _ctx())
    assert files == {} and sources == ["/skills/"]


# --- compile wiring ---
# `create_agent` returns a compiled graph that no longer exposes its middleware, so capture the
# stack the factory builds at the boundary instead of introspecting the result.
@pytest.fixture
def built(monkeypatch):
    import langchain.agents as lc_agents

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(lc_agents, "create_agent", _capture)

    def _build(config, ctx=None):
        agent_factory(config, ctx or _ctx(skill_library=index_skills([RESEARCH])))
        mws = captured.get("middleware") or []
        return {
            "names": [type(mw).__name__ for mw in mws],
            "middleware": {type(mw).__name__: mw for mw in mws},
        }

    return _build


def _fs_tools(built_stack) -> set[str]:
    fs = built_stack["middleware"]["FilesystemMiddleware"]
    return {t.name for t in fs.tools}


def test_skills_attach_skills_and_filesystem_middleware(built):
    """Skills are useless without read_file — the prompt tells the model to open the SKILL.md
    path — so a skills-enabled node gets a filesystem even when the author didn't enable one."""
    names = built({"flavor": "deep_agent", "model": "fake", "skills": ["sk-1"]})["names"]
    assert "SkillsMiddleware" in names
    assert "FilesystemMiddleware" in names


def test_skills_alone_do_not_grant_a_writable_filesystem(built):
    tools = _fs_tools(built({"flavor": "deep_agent", "model": "fake", "skills": ["sk-1"]}))
    assert {"ls", "read_file"} <= tools
    assert not ({"write_file", "edit_file", "delete", "execute"} & tools)


def test_explicit_filesystem_still_gets_full_tools(built):
    tools = _fs_tools(built({
        "flavor": "deep_agent", "model": "fake", "skills": ["sk-1"],
        "filesystem": {"enabled": True},
    }))
    assert "write_file" in tools


def test_skill_mount_is_write_denied(built):
    """Defence in depth over SkillLibraryBackend's own refusal: the library is a tenant-scoped
    record, so the agent must not rewrite a skill mid-run."""
    fs = built({"flavor": "deep_agent", "model": "fake", "skills": ["sk-1"]})["middleware"]["FilesystemMiddleware"]
    rules = [r for r in (fs._permissions or []) if r.mode == "deny"]
    assert any(f"{SKILLS_ROOT}**" in r.paths and "write" in r.operations for r in rules)


def test_no_skills_configured_changes_nothing(built):
    names = built({"flavor": "deep_agent", "model": "fake"})["names"]
    assert "SkillsMiddleware" not in names
    assert "FilesystemMiddleware" not in names
