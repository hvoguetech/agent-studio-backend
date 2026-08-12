"""Supabase Management API client (managed-backend provisioning).

A thin async wrapper over https://api.supabase.com used by services/backend_provisioning.py to
create + inspect a managed Supabase project (Postgres + auth + storage) for an agent. Network I/O
is isolated here so the provisioning service stays unit-testable with a fake httpx transport
(pass a `client` built on `httpx.MockTransport`).

Ported from the atlas builder's sync `supabase_mgmt.py`, made async + extended (health wait,
connection string, api-key helpers).

⚠️ LIVE-ITERATION POINTS (verify against real Supabase creds on first use; isolated to this file):
- the connection-string form — `connection_string()` returns the DIRECT URL constructed from the
  ref + the db password we set at create time. The pooled/Supavisor URL (region-specific host,
  recommended for serverless) is a follow-up.
- the api-keys response shape across the legacy (`anon`/`service_role`) and new
  (`sb_publishable_`/`sb_secret_`) key schemes — handled defensively below.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MGMT_API = "https://api.supabase.com"
_TIMEOUT = 30.0
HEALTHY = "ACTIVE_HEALTHY"


class SupabaseError(RuntimeError):
    """A Supabase Management API call failed."""


class SupabaseTimeout(SupabaseError):
    """A freshly-created project did not become healthy within the timeout."""


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _send(
    client: httpx.AsyncClient, method: str, path: str, token: str,
    *, json: dict | None = None, ok: tuple[int, ...] = (200, 201),
) -> Any:
    """Issue a request and return the parsed JSON body (or None for empty/204)."""
    try:
        resp = await client.request(method, path, headers=_headers(token), json=json)
    except httpx.HTTPError as e:  # connect/timeout/etc.
        raise SupabaseError(f"{method} {path} failed: {e}") from e
    if resp.status_code not in ok:
        raise SupabaseError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


async def list_organizations(token: str, *, client: httpx.AsyncClient) -> list[dict]:
    """[{id, name, ...}] for the token's account."""
    return await _send(client, "GET", "/v1/organizations", token) or []


async def create_project(
    token: str, *, organization_id: str, name: str, db_pass: str,
    region: str, plan: str = "free", client: httpx.AsyncClient,
) -> dict:
    """Create a managed Supabase project. Returns the API project object (its `id` is the ref).
    Creation is async — the project reports `COMING_UP` and becomes `ACTIVE_HEALTHY` minutes later
    (poll with `wait_until_healthy`)."""
    body = {
        "organization_id": organization_id,
        "name": name,
        "db_pass": db_pass,
        "region": region,
        "plan": plan,
    }
    return await _send(client, "POST", "/v1/projects", token, json=body)


async def get_project(token: str, ref: str, *, client: httpx.AsyncClient) -> dict | None:
    """Fetch a project (for status polling), or None if it 404s."""
    try:
        resp = await client.request("GET", f"/v1/projects/{ref}", headers=_headers(token))
    except httpx.HTTPError as e:
        raise SupabaseError(f"GET /v1/projects/{ref} failed: {e}") from e
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise SupabaseError(f"GET /v1/projects/{ref} -> {resp.status_code}: {resp.text[:500]}")
    return resp.json()


async def wait_until_healthy(
    token: str, ref: str, *, client: httpx.AsyncClient, timeout_s: int, interval_s: int,
) -> dict:
    """Poll `get_project` until status == ACTIVE_HEALTHY; raise SupabaseTimeout past `timeout_s`."""
    waited = 0
    while True:
        proj = await get_project(token, ref, client=client)
        if proj and proj.get("status") == HEALTHY:
            return proj
        if waited >= timeout_s:
            raise SupabaseTimeout(
                f"project {ref} not healthy after {timeout_s}s "
                f"(last status: {(proj or {}).get('status')!r})"
            )
        await asyncio.sleep(interval_s)
        waited += interval_s


async def get_api_keys(token: str, ref: str, *, client: httpx.AsyncClient) -> list[dict]:
    """[{name, api_key, ...}] — the project's client + service keys."""
    return await _send(client, "GET", f"/v1/projects/{ref}/api-keys", token) or []


def anon_key(keys: list[dict]) -> str | None:
    """The publishable (client-safe) key across both schemes: legacy name=='anon', or a new-scheme
    `sb_publishable_` key. Never the service_role/secret key."""
    for k in keys:
        if k.get("name") == "anon":
            return k.get("api_key")
    for k in keys:
        v = k.get("api_key") or ""
        if v.startswith("sb_publishable_") or "publishable" in (k.get("name") or "").lower():
            return v
    return None


def service_role_key(keys: list[dict]) -> str | None:
    """The service_role (secret) key across both schemes: legacy name=='service_role', or a
    new-scheme `sb_secret_` key. Server-only — store as a secret ref, never return to a model."""
    for k in keys:
        if k.get("name") == "service_role":
            return k.get("api_key")
    for k in keys:
        v = k.get("api_key") or ""
        if v.startswith("sb_secret_") or "secret" in (k.get("name") or "").lower():
            return v
    return None


def connection_string(ref: str, db_pass: str) -> str:
    """Direct Postgres connection URL, deterministic from the ref + the db password we set at
    create time. NOTE (live-iteration): the pooled/Supavisor URL (region-specific host) is a
    follow-up; verify direct-host IPv4/IPv6 behavior against live Supabase before relying on it."""
    return f"postgresql://postgres:{db_pass}@db.{ref}.supabase.co:5432/postgres"


async def delete_project(token: str, ref: str, *, client: httpx.AsyncClient) -> None:
    """Delete a project (idempotent — a 404 is treated as already-gone)."""
    await _send(client, "DELETE", f"/v1/projects/{ref}", token, ok=(200, 204, 404))


# ── Storage (data-plane on the PROJECT host, authenticated with the service_role key) ────────────
def _svc_headers(service_role_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {service_role_key}",
        "apikey": service_role_key,
        "Content-Type": "application/json",
    }


async def create_storage_bucket(
    service_role_key: str, *, name: str, public: bool = False, client: httpx.AsyncClient
) -> dict:
    """Create a Storage bucket. This is a DATA-PLANE call on the project host
    (`POST https://<ref>.supabase.co/storage/v1/bucket`) with the service_role key — NOT the
    Management API — so `client` must be built with base_url = the project endpoint. Idempotent: an
    existing bucket (409 / "already exists") is treated as success."""
    body = {"id": name, "name": name, "public": bool(public)}
    try:
        resp = await client.request("POST", "/storage/v1/bucket", headers=_svc_headers(service_role_key), json=body)
    except httpx.HTTPError as e:
        raise SupabaseError(f"create bucket {name!r} failed: {e}") from e
    if resp.status_code in (200, 201):
        return resp.json() if resp.content else {"name": name}
    if resp.status_code == 409 or (resp.status_code == 400 and "already exists" in resp.text.lower()):
        return {"name": name, "existing": True}
    raise SupabaseError(f"create bucket {name!r} -> {resp.status_code}: {resp.text[:300]}")


# ── Edge functions (Management API) ───────────────────────────────────────────────────────────
async def list_edge_functions(token: str, ref: str, *, client: httpx.AsyncClient) -> list[dict]:
    """[{slug, name, status, ...}] for the project."""
    return await _send(client, "GET", f"/v1/projects/{ref}/functions", token) or []


async def deploy_edge_function(
    token: str, ref: str, *, slug: str, source: str, verify_jwt: bool = True, client: httpx.AsyncClient
) -> dict:
    """Create/deploy an edge function with inline TypeScript `source`.

    ⚠️ LIVE-VERIFY: the Management API deploy shape has moved across versions (older: this inline
    `{slug,name,body}` create; newer: an eszip bundle upload). This uses the inline-body form; swap
    to the bundle endpoint if the target Supabase version requires it."""
    body = {"slug": slug, "name": slug, "body": source, "verify_jwt": bool(verify_jwt)}
    return await _send(client, "POST", f"/v1/projects/{ref}/functions", token, json=body)


# ── Auth config (Management API) ──────────────────────────────────────────────────────────────
async def update_auth_config(token: str, ref: str, *, patch: dict, client: httpx.AsyncClient) -> dict:
    """PATCH the project's auth config (`/v1/projects/{ref}/config/auth`). `patch` is a partial
    config dict, e.g. {'site_url': 'https://app.example.com', 'external_email_enabled': True}."""
    return await _send(client, "PATCH", f"/v1/projects/{ref}/config/auth", token, json=patch, ok=(200,))
