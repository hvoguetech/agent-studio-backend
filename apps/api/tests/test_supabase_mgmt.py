"""Supabase Management client (services/providers/supabase_mgmt.py) — fake-transport unit tests."""

from __future__ import annotations

import httpx
import pytest

import ros.services.providers.supabase_mgmt as supa


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=supa.MGMT_API, transport=httpx.MockTransport(handler))


async def test_create_then_wait_until_healthy_polls_until_active():
    state = {"gets": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v1/projects":
            return httpx.Response(201, json={"id": "ref1", "status": "COMING_UP"})
        if req.method == "GET" and req.url.path == "/v1/projects/ref1":
            state["gets"] += 1
            status = "ACTIVE_HEALTHY" if state["gets"] >= 2 else "COMING_UP"
            return httpx.Response(200, json={"id": "ref1", "status": status})
        return httpx.Response(404, json={})

    async with _client(handler) as c:
        created = await supa.create_project(
            "tok", organization_id="o", name="n", db_pass="p", region="us-east-1", client=c
        )
        assert created["id"] == "ref1"
        proj = await supa.wait_until_healthy("tok", "ref1", client=c, timeout_s=10, interval_s=0)
        assert proj["status"] == supa.HEALTHY
        assert state["gets"] >= 2  # polled more than once


async def test_wait_until_healthy_times_out():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "r", "status": "COMING_UP"})

    async with _client(handler) as c:
        with pytest.raises(supa.SupabaseTimeout):
            await supa.wait_until_healthy("tok", "r", client=c, timeout_s=0, interval_s=0)


async def test_get_project_404_returns_none():
    async with _client(lambda req: httpx.Response(404, json={})) as c:
        assert await supa.get_project("tok", "missing", client=c) is None


async def test_non_2xx_raises_supabase_error():
    async with _client(lambda req: httpx.Response(500, text="boom")) as c:
        with pytest.raises(supa.SupabaseError):
            await supa.create_project(
                "tok", organization_id="o", name="n", db_pass="p", region="r", client=c
            )


def test_api_key_helpers_handle_both_schemes():
    legacy = [{"name": "anon", "api_key": "a"}, {"name": "service_role", "api_key": "s"}]
    assert supa.anon_key(legacy) == "a"
    assert supa.service_role_key(legacy) == "s"
    new = [
        {"name": "publishable key", "api_key": "sb_publishable_x"},
        {"name": "secret key", "api_key": "sb_secret_y"},
    ]
    assert supa.anon_key(new) == "sb_publishable_x"
    assert supa.service_role_key(new) == "sb_secret_y"
    # anon_key never returns the secret key
    assert supa.anon_key([{"name": "service_role", "api_key": "s"}]) is None


def test_connection_string_is_deterministic():
    assert (
        supa.connection_string("refX", "pw")
        == "postgresql://postgres:pw@db.refX.supabase.co:5432/postgres"
    )


async def test_delete_project_tolerates_404():
    async with _client(lambda req: httpx.Response(404, json={})) as c:
        await supa.delete_project("tok", "gone", client=c)  # no raise


# --- storage / edge functions / auth (the rest of the primitive) ---
async def test_create_storage_bucket_success():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/storage/v1/bucket":
            return httpx.Response(200, json={"name": "uploads"})
        return httpx.Response(404, json={})

    async with _client(handler) as c:
        out = await supa.create_storage_bucket("svc_key", name="uploads", public=False, client=c)
        assert out["name"] == "uploads"


async def test_create_storage_bucket_already_exists_is_ok():
    async with _client(lambda req: httpx.Response(409, json={"error": "already exists"})) as c:
        out = await supa.create_storage_bucket("svc_key", name="uploads", client=c)
        assert out.get("existing") is True


async def test_create_storage_bucket_error_raises():
    async with _client(lambda req: httpx.Response(500, text="boom")) as c:
        with pytest.raises(supa.SupabaseError):
            await supa.create_storage_bucket("svc_key", name="x", client=c)


async def test_deploy_edge_function_posts_source():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v1/projects/ref1/functions":
            import json as _j
            seen["body"] = _j.loads(req.content)
            return httpx.Response(201, json={"slug": "ingest", "status": "ACTIVE"})
        return httpx.Response(404, json={})

    async with _client(handler) as c:
        out = await supa.deploy_edge_function("tok", "ref1", slug="ingest", source="export default () => {}", client=c)
        assert out["slug"] == "ingest"
    assert seen["body"]["slug"] == "ingest"
    assert seen["body"]["body"] == "export default () => {}"  # source carried as `body`


async def test_update_auth_config_patches():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PATCH" and req.url.path == "/v1/projects/ref1/config/auth":
            return httpx.Response(200, json={"site_url": "https://x"})
        return httpx.Response(404, json={})

    async with _client(handler) as c:
        out = await supa.update_auth_config("tok", "ref1", patch={"site_url": "https://x"}, client=c)
        assert out["site_url"] == "https://x"
