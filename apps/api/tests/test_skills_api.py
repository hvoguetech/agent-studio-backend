"""Skill library router: CRUD, the Agent Skills name rules at the boundary, duplicate names,
tenant isolation, and the skill:read/write authz split."""

from __future__ import annotations

import uuid

import httpx
import pytest

from ros.main import create_app
from ros.security import create_access_token
from ros.services.auth import AuthService

BASE = "/v1/projects/p1/skills"
BODY = {"name": "web-research", "description": "Research the web", "content": "# do it"}


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


async def test_crud_round_trip():
    tok = await _token("owner")
    async with _client() as c:
        r = await c.post(BASE, json=BODY, headers=_auth(tok))
        assert r.status_code == 201, r.text
        skill = r.json()
        assert skill["name"] == "web-research" and skill["enabled"] is True and skill["version"] == 1

        r = await c.get(BASE, headers=_auth(tok))
        assert r.status_code == 200 and any(s["id"] == skill["id"] for s in r.json())

        r = await c.get(f"{BASE}/{skill['id']}", headers=_auth(tok))
        assert r.status_code == 200 and r.json()["content"] == "# do it"

        r = await c.patch(f"{BASE}/{skill['id']}", json={"content": "# revised"}, headers=_auth(tok))
        assert r.status_code == 200
        assert r.json()["content"] == "# revised" and r.json()["version"] == 2  # content bumps it

        r = await c.patch(f"{BASE}/{skill['id']}", json={"enabled": False}, headers=_auth(tok))
        assert r.json()["enabled"] is False and r.json()["version"] == 2  # a flip does not

        assert (await c.delete(f"{BASE}/{skill['id']}", headers=_auth(tok))).status_code == 204
        assert (await c.get(f"{BASE}/{skill['id']}", headers=_auth(tok))).status_code == 404


@pytest.mark.parametrize("name", ["Web-Research", "web_research", "-lead", "a--b", "x" * 65])
async def test_invalid_names_are_rejected(name):
    """A name the Agent Skills spec rejects must fail at save time — at runtime it would just
    silently never load."""
    tok = await _token("owner")
    async with _client() as c:
        r = await c.post(BASE, json={**BODY, "name": name}, headers=_auth(tok))
        assert r.status_code == 422, r.text


async def test_duplicate_name_conflicts():
    tok = await _token("owner")
    async with _client() as c:
        assert (await c.post(BASE, json=BODY, headers=_auth(tok))).status_code == 201
        r = await c.post(BASE, json=BODY, headers=_auth(tok))
        assert r.status_code == 409  # the name is the mount directory; two would shadow


async def test_another_tenant_cannot_see_or_fetch_it():
    tok_a, tok_b = await _token("owner"), await _token("owner")
    async with _client() as c:
        created = (await c.post(BASE, json=BODY, headers=_auth(tok_a))).json()
        assert (await c.get(BASE, headers=_auth(tok_b))).json() == []
        assert (await c.get(f"{BASE}/{created['id']}", headers=_auth(tok_b))).status_code == 404


async def test_viewer_can_read_but_not_write():
    tok = await _token("viewer")
    async with _client() as c:
        assert (await c.get(BASE, headers=_auth(tok))).status_code == 200
        assert (await c.post(BASE, json=BODY, headers=_auth(tok))).status_code == 403
