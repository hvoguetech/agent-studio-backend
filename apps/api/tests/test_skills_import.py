"""Importing Claude-Code-style skill folders into the library.

ROS skills and Claude Code skills are the same Agent Skills artifact, so these tests care about
the parse (frontmatter, folder layout, name/directory disagreement) and about the two things an
import must never do: overwrite an existing skill, or quietly lose frontmatter it can't keep.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from ros.main import create_app
from ros.security import create_access_token
from ros.services.auth import AuthService
from ros.skills import SkillImportError, parse_skill_tree, split_frontmatter

BASE = "/v1/projects/p1/skills"

TREE = {
    "web-research/SKILL.md": (
        "---\nname: web-research\ndescription: Structured web research\n---\n# Web research\n\n1. Search\n"
    ),
    "web-research/checklist.md": "- cite sources",
    "code-review/SKILL.md": "---\nname: code-review\ndescription: Review a diff\n---\n# Review\n",
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _token(role: str = "owner") -> str:
    from ros.db import SessionLocal

    async with SessionLocal() as s:
        owner = await AuthService.register(s, email=f"u{uuid.uuid4().hex[:10]}@example.com",
                                           password="ownerpass1")
        user = owner if role == "owner" else await AuthService.invite(
            s, tenant_id=owner.tenant_id, email=f"u{uuid.uuid4().hex[:10]}@example.com",
            role=role, password="memberpass1",
        )
    return create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role)


# --- frontmatter parsing ---
def test_split_frontmatter_extracts_meta_and_body():
    meta, body = split_frontmatter("---\nname: x\ndescription: does a thing\n---\n# Title\ntext\n")
    assert meta == {"name": "x", "description": "does a thing"}
    assert body == "# Title\ntext\n"


def test_body_without_frontmatter_is_kept_whole():
    meta, body = split_frontmatter("# Just markdown\n")
    assert meta == {} and body == "# Just markdown\n"


def test_broken_yaml_does_not_lose_the_file():
    """Better to import the text and let the author fix the metadata than to reject it."""
    meta, body = split_frontmatter("---\nname: [unclosed\n---\nbody\n")
    assert meta == {}
    assert "body" in body


# --- tree parsing ---
def test_parses_each_skill_folder_with_its_supporting_files():
    parsed, skipped = parse_skill_tree(TREE)
    by_name = {p["name"]: p for p in parsed}
    assert set(by_name) == {"web-research", "code-review"}
    assert by_name["web-research"]["files"] == {"checklist.md": "- cite sources"}
    assert by_name["web-research"]["content"].startswith("# Web research")
    assert skipped == []


def test_directory_name_wins_when_frontmatter_name_is_unusable():
    """The spec requires name == directory, and the mount uses the directory — so an import that
    trusted a bad frontmatter name would produce a skill that never loads."""
    parsed, _ = parse_skill_tree({"my-skill/SKILL.md": "---\nname: Not A Slug\ndescription: d\n---\nbody"})
    assert parsed[0]["name"] == "my-skill"


def test_skill_with_no_usable_name_at_all_is_reported():
    parsed, skipped = parse_skill_tree({"Bad Dir/SKILL.md": "---\ndescription: d\n---\nbody"})
    assert parsed == []
    assert skipped and "no usable skill name" in skipped[0]["reason"]


def test_files_outside_a_skill_folder_are_reported_not_dropped():
    _, skipped = parse_skill_tree({**TREE, "README.md": "not a skill"})
    assert [s["path"] for s in skipped] == ["README.md"]


def test_a_tree_with_no_skill_md_is_an_error():
    with pytest.raises(SkillImportError, match="no SKILL.md found"):
        parse_skill_tree({"notes.md": "hello"})


def test_extra_frontmatter_keys_are_recorded_as_dropped():
    parsed, _ = parse_skill_tree({
        "x/SKILL.md": "---\nname: x\ndescription: d\nallowed-tools: [read_file]\nlicense: MIT\n---\nbody",
    })
    assert parsed[0]["dropped_frontmatter"] == ["allowed-tools", "license"]


def test_supporting_file_cannot_climb_out_of_the_skill_folder():
    parsed, _ = parse_skill_tree({"x/SKILL.md": "---\nname: x\ndescription: d\n---\nb", "x/../evil": "no"})
    assert parsed[0]["files"] == {}


# --- the route ---
async def test_import_creates_attachable_skills():
    tok = await _token("owner")
    async with _client() as c:
        r = await c.post(f"{BASE}/import", json={"files": TREE}, headers=_auth(tok))
        assert r.status_code == 200, r.text
        report = r.json()
        assert report["imported"] == 2 and report["skipped"] == 0

        listed = {s["name"]: s for s in (await c.get(BASE, headers=_auth(tok))).json()}
        assert set(listed) == {"web-research", "code-review"}
        assert listed["web-research"]["files"] == {"checklist.md": "- cite sources"}


async def test_import_renames_instead_of_overwriting():
    """An import must never replace a skill an agent is already attached to."""
    tok = await _token("owner")
    async with _client() as c:
        await c.post(BASE, json={"name": "web-research", "description": "mine", "content": "keep me"},
                     headers=_auth(tok))
        r = await c.post(f"{BASE}/import", json={"files": TREE}, headers=_auth(tok))
        report = r.json()

        renamed = [i for i in report["items"] if i.get("renamed_from") == "web-research"]
        assert renamed and renamed[0]["name"] == "web-research-2"
        assert any("already existed" in w for w in report["warnings"])

        listed = {s["name"]: s for s in (await c.get(BASE, headers=_auth(tok))).json()}
        assert listed["web-research"]["content"] == "keep me"  # the original is untouched


async def test_import_warns_about_frontmatter_it_cannot_keep():
    tok = await _token("owner")
    tree = {"x/SKILL.md": "---\nname: x\ndescription: d\nallowed-tools: [read_file]\n---\nbody"}
    async with _client() as c:
        report = (await c.post(f"{BASE}/import", json={"files": tree}, headers=_auth(tok))).json()
    assert any("allowed-tools" in w for w in report["warnings"])
    assert report["items"][0]["dropped_frontmatter"] == ["allowed-tools"]


async def test_import_rejects_a_folder_that_holds_no_skills():
    tok = await _token("owner")
    async with _client() as c:
        r = await c.post(f"{BASE}/import", json={"files": {"notes.md": "hi"}}, headers=_auth(tok))
    assert r.status_code == 422 and "SKILL.md" in r.text


async def test_viewer_cannot_import():
    tok = await _token("viewer")
    async with _client() as c:
        r = await c.post(f"{BASE}/import", json={"files": TREE}, headers=_auth(tok))
    assert r.status_code == 403


async def test_an_imported_skill_mounts_for_an_agent():
    """End to end: a Claude Code folder becomes a skill deepagents can discover."""
    from deepagents.backends.state import StateBackend
    from deepagents.middleware.skills import _list_skills_with_errors

    from ros.db import SessionLocal
    from ros.security import decode_token
    from ros.services.skills import load_skill_library
    from ros.skills import SKILLS_ROOT, materialize, mount

    tok = await _token("owner")
    tenant_id = decode_token(tok)["tid"]
    async with _client() as c:
        await c.post(f"{BASE}/import", json={"files": TREE}, headers=_auth(tok))

    async with SessionLocal() as s:
        library = await load_skill_library(s, tenant_id, "p1")

    rows = [library[n] for n in ("web-research", "code-review")]
    found, err = _list_skills_with_errors(mount(materialize(rows), StateBackend()), SKILLS_ROOT)
    assert err is None
    assert sorted(f["name"] for f in found) == ["code-review", "web-research"]
    assert dict((f["name"], f["description"]) for f in found)["web-research"] == "Structured web research"
