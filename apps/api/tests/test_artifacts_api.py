"""WS7 phase 2 - artifact router: upload/list/download/delete over the local backend, plus
the artifact:read/write authz split."""

from __future__ import annotations

import uuid

import httpx
import pytest

from ros.artifacts import reset_artifact_store
from ros.config import settings
from ros.main import create_app
from ros.security import create_access_token
from ros.services.auth import AuthService


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


def _email() -> str:
    return f"u{uuid.uuid4().hex[:10]}@example.com"


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _token(role: str = "owner") -> str:
    from ros.db import SessionLocal

    async with SessionLocal() as s:
        owner = await AuthService.register(s, email=_email(), password="ownerpass1")
        user = owner if role == "owner" else await AuthService.invite(
            s, tenant_id=owner.tenant_id, email=_email(), role=role, password="memberpass1"
        )
    return create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role)


@pytest.fixture(autouse=True)
def _local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "artifact_store", "local")
    monkeypatch.setattr(settings, "artifact_local_dir", str(tmp_path))
    reset_artifact_store()
    yield
    reset_artifact_store()


async def test_upload_list_download_delete():
    tok = await _token("owner")
    base = "/v1/projects/p1/artifacts"
    async with _client() as c:
        r = await c.post(base, files={"file": ("report.txt", b"hello world", "text/plain")},
                         data={"run_id": "r1"}, headers=_auth(tok))
        assert r.status_code == 201, r.text
        art = r.json()
        assert art["size"] == 11 and art["filename"] == "report.txt" and len(art["sha256"]) == 64
        aid = art["id"]

        r = await c.get(base, headers=_auth(tok))
        assert r.status_code == 200 and any(a["id"] == aid for a in r.json())

        r = await c.get(f"{base}/{aid}/download", headers=_auth(tok))
        assert r.status_code == 200 and r.content == b"hello world"
        assert "attachment" in r.headers.get("content-disposition", "")

        assert (await c.delete(f"{base}/{aid}", headers=_auth(tok))).status_code == 204
        r = await c.get(base, headers=_auth(tok))
        assert all(a["id"] != aid for a in r.json())


async def test_download_404_for_missing():
    tok = await _token("owner")
    async with _client() as c:
        r = await c.get("/v1/projects/p1/artifacts/nope/download", headers=_auth(tok))
        assert r.status_code == 404


async def _owner_token_and_tenant() -> tuple[str, str]:
    from ros.db import SessionLocal

    async with SessionLocal() as s:
        owner = await AuthService.register(s, email=_email(), password="ownerpass1")
    return create_access_token(user_id=owner.id, tenant_id=owner.tenant_id, role=owner.role), owner.tenant_id


async def test_download_ref_streams_in_scope_and_rejects_out_of_scope():
    """The by-ref download (for artifact-plane files with no DB row) streams an in-scope ref and
    403s a key outside the caller's tenant/project prefix — the cross-project guard."""
    from ros.artifacts import get_artifact_store

    tok, tenant = await _owner_token_and_tenant()
    ref = await get_artifact_store().put(
        tenant_id=tenant, project_id="p1", run_id="r1",
        data=b"<h1>Mock</h1>", filename="mockup.html", content_type="text/html",
    )
    base = "/v1/projects/p1/artifacts/download-ref"
    async with _client() as c:
        r = await c.get(base, params={"bucket": ref.bucket, "key": ref.key,
                                      "filename": "mockup.html", "content_type": "text/html"},
                        headers=_auth(tok))
        assert r.status_code == 200, r.text
        assert r.content == b"<h1>Mock</h1>"
        assert "attachment" in r.headers.get("content-disposition", "")

        r2 = await c.get(base, params={"bucket": ref.bucket,
                                       "key": "env/othertenant/otherproj/r1/sha/evil.html"},
                         headers=_auth(tok))
        assert r2.status_code == 403, r2.text


async def test_viewer_can_read_but_not_write():
    tok = await _token("viewer")
    base = "/v1/projects/p1/artifacts"
    async with _client() as c:
        r = await c.post(base, files={"file": ("x.txt", b"x", "text/plain")}, headers=_auth(tok))
        assert r.status_code == 403, r.text          # artifact:write = editor
        assert (await c.get(base, headers=_auth(tok))).status_code == 200  # artifact:read = viewer
