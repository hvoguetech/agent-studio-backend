"""Default-deny authorization seam (B/E4, #14).

Covers the three guarantees:
1. structural coverage — every route declares a permission or is explicitly public;
2. the chokepoint — `authorize()` is default-deny (unknown permission => denied);
3. enforcement — under-privileged callers are 403'd (notably the run-execution hole
   a viewer/connector could previously exploit), self-service works for any role, and
   an undeclared route fails closed via the app-level guard.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from ros.authz import (
    ANY,
    PERMISSIONS,
    Subject,
    audit_route_coverage,
    authorize,
    default_deny_guard,
    public_endpoint,
    require_permission,
)
from ros.config import settings
from ros.main import create_app
from ros.security import create_access_token
from ros.services.auth import AuthError, AuthService


def _client(app=None) -> httpx.AsyncClient:
    app = app or create_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _email() -> str:
    return f"u{uuid.uuid4().hex[:10]}@example.com"


# --------------------------------------------------------------------------- #
# 1. Structural coverage
# --------------------------------------------------------------------------- #
def test_every_route_declares_authz():
    """Default-deny is structural: no route may be reachable without declaring a
    permission or being explicitly marked public."""
    undeclared = audit_route_coverage(create_app())
    assert undeclared == [], "routes missing an authz declaration:\n" + "\n".join(
        f"  {','.join(m)} {p} [{n}]" for m, p, n in undeclared
    )


def test_require_permission_rejects_unknown_permission():
    """A typo'd / unregistered permission is caught when the route is built, not at
    request time."""
    with pytest.raises(RuntimeError):
        require_permission("does_not:exist")


def test_run_execution_is_editor_gated():
    """The specific live hole B/E4 exists to close: executing a run must require editor+,
    never viewer/connector."""
    for perm in ("run:execute",):
        assert PERMISSIONS[perm] == "editor"


# --------------------------------------------------------------------------- #
# 2. The authorize() chokepoint is default-deny
# --------------------------------------------------------------------------- #
def _subj(role: str) -> Subject:
    return Subject(id="u1", tenant_id="t1", role=role)


def test_authorize_denies_unknown_permission():
    from fastapi import HTTPException

    # unknown permission -> denied regardless of role (even owner)
    with pytest.raises(HTTPException) as ei:
        authorize(_subj("owner"), "totally:madeup")
    assert ei.value.status_code == 403


def test_authorize_enforces_role_tiers():
    from fastapi import HTTPException

    authorize(_subj("owner"), "run:execute")  # owner >= editor -> ok
    authorize(_subj("editor"), "run:execute")  # editor -> ok
    for role in ("viewer", "connector"):
        with pytest.raises(HTTPException):
            authorize(_subj(role), "run:execute")


def test_authorize_any_grants_every_authenticated_role():
    assert PERMISSIONS["account:self"] == ANY
    for role in ("owner", "admin", "editor", "viewer", "connector"):
        authorize(_subj(role), "account:self")  # never raises


# --------------------------------------------------------------------------- #
# 3. Enforcement over HTTP with real, role-scoped tokens
# --------------------------------------------------------------------------- #
async def _token(role: str) -> str:
    """Mint an access token for a fresh active user of `role`. `owner` gets a new
    workspace; other roles are invited into it (so they are active with a role)."""
    from ros.db import SessionLocal

    async with SessionLocal() as s:
        owner = await AuthService.register(s, email=_email(), password="ownerpass1")
        if role == "owner":
            user = owner
        else:
            try:
                user = await AuthService.invite(
                    s, tenant_id=owner.tenant_id, email=_email(), role=role, password="memberpass1"
                )
            except AuthError:
                raise
    return create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_viewer_and_connector_cannot_execute_runs():
    """The core B/E4 fix: a viewer or connector (or a viewer-scoped key) hitting the run
    surface is 403'd by authz BEFORE any run is created."""
    async with _client() as c:
        run_url = "/v1/projects/p1/workflows/w1/runs"
        proj_run_url = "/v1/projects/p1/run"
        for role in ("viewer", "connector"):
            tok = await _token(role)
            r = await c.post(run_url, json={"input": {}}, headers=_auth(tok))
            assert r.status_code == 403, f"{role} create_run: {r.status_code} {r.text}"
            r = await c.post(proj_run_url, json={"input": {}}, headers=_auth(tok))
            assert r.status_code == 403, f"{role} project_run: {r.status_code} {r.text}"


async def test_editor_passes_run_authorization():
    """An editor is NOT blocked by authz on the run surface. The body then fails for an
    unrelated reason (project p1 doesn't exist -> 500), which is exactly the point: we got
    PAST the run:execute gate. raise_app_exceptions=False turns that into a 500 response so
    we can assert it is simply never 403."""
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        tok = await _token("editor")
        r = await c.post(
            "/v1/projects/p1/workflows/w1/runs", json={"input": {}}, headers=_auth(tok)
        )
        assert r.status_code != 403, r.text


async def test_viewer_can_read_but_not_write_workflows():
    async with _client() as c:
        tok = await _token("viewer")
        # read (workflow:read = viewer) -> not forbidden
        r = await c.get("/v1/projects/p1/workflows", headers=_auth(tok))
        assert r.status_code != 403, r.text
        # write (workflow:write = editor) -> forbidden for viewer
        r = await c.post(
            "/v1/projects/p1/workflows", json={"name": "x", "graph": {}}, headers=_auth(tok)
        )
        assert r.status_code == 403, r.text


async def test_connector_can_manage_own_mcp_tokens():
    """`connector` is the least-privileged role; its whole purpose is self-service MCP
    tokens, gated as `mcp_token:self` (ANY authenticated)."""
    async with _client() as c:
        tok = await _token("connector")
        r = await c.get("/v1/projects/p1/mcp-tokens", headers=_auth(tok))
        assert r.status_code != 403, r.text


async def test_anonymous_is_unauthorized_on_gated_route(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    async with _client() as c:
        r = await c.get("/v1/projects/p1/workflows")
        assert r.status_code == 401, r.text


async def test_catalog_endpoints_are_no_longer_public(monkeypatch):
    """The model / node-type catalogs were tightened from public to viewer-read:
    anonymous is 401, an authenticated viewer gets them."""
    monkeypatch.setattr(settings, "auth_required", True)
    async with _client() as c:
        for path in ("/v1/models", "/v1/node-types"):
            assert (await c.get(path)).status_code == 401, path
        tok = await _token("viewer")
        for path in ("/v1/models", "/v1/node-types"):
            assert (await c.get(path, headers=_auth(tok))).status_code == 200, path


# --------------------------------------------------------------------------- #
# 3b. The app-level guard fails closed for an UNDECLARED route
# --------------------------------------------------------------------------- #
async def test_guard_denies_undeclared_route():
    from fastapi import Depends, FastAPI

    app = FastAPI(dependencies=[Depends(default_deny_guard)])

    @app.get("/declared", dependencies=[Depends(public_endpoint)])
    async def declared():
        return {"ok": True}

    @app.get("/undeclared")  # intentionally no permission / public marker
    async def undeclared():
        return {"ok": True}

    async with _client(app) as c:
        assert (await c.get("/declared")).status_code == 200
        assert (await c.get("/undeclared")).status_code == 403
