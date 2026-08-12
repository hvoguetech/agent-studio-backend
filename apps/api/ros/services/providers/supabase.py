"""Supabase resource provider — a dedicated managed project (Postgres + auth + storage + edge fns).

Implements the ResourceProvider seam. Owns all Supabase external I/O (via supabase_mgmt); the
orchestrator (services/backend_provisioning) stores the returned creds as secret refs and records
the row. The non-DB extras (buckets / auth / edge functions) are applied best-effort — a failure
there is reported in `config`, never fatal to the healthy database.
"""

from __future__ import annotations

import logging
import secrets as _secrets

import httpx

from ros.config import settings
from ros.services.providers import supabase_mgmt as supa
from ros.services.providers.base import ProvisionError, ProvisionOutcome

logger = logging.getLogger(__name__)


class SupabaseProvider:
    kind = "supabase"

    def is_enabled(self) -> bool:
        return bool(settings.supabase_management_token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=supa.MGMT_API, timeout=supa._TIMEOUT)

    async def _resolve_org(self, token: str, client: httpx.AsyncClient) -> str:
        if settings.supabase_default_org_id:
            return settings.supabase_default_org_id
        orgs = await supa.list_organizations(token, client=client)
        if not orgs:
            raise ProvisionError("no Supabase organization available for the management token")
        return orgs[0]["id"]

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        token = settings.supabase_management_token
        region = spec.get("region") or settings.supabase_default_region
        db_pass = _secrets.token_urlsafe(24)

        # Create the project + gather creds. On any failure after create, delete the project so
        # nothing leaks, then raise (the orchestrator has written nothing yet).
        async with self._client() as client:
            org_id = await self._resolve_org(token, client)
            created = await supa.create_project(
                token, organization_id=org_id, name=name, db_pass=db_pass, region=region, client=client
            )
            ref = created.get("id") or created.get("ref")
            if not ref:
                raise ProvisionError(f"Supabase create_project returned no ref: {created!r}")
            try:
                await supa.wait_until_healthy(
                    token, ref, client=client,
                    timeout_s=settings.supabase_provision_timeout_s,
                    interval_s=settings.supabase_poll_interval_s,
                )
                keys = await supa.get_api_keys(token, ref, client=client)
                anon = supa.anon_key(keys)
                service = supa.service_role_key(keys)
                db_url = supa.connection_string(ref, db_pass)
                endpoint = f"https://{ref}.supabase.co"
            except Exception as e:
                await self._safe_delete(token, ref)
                raise ProvisionError(f"provisioning failed after create ({ref}): {e}") from e

        # Best-effort extras — a bucket/auth/function failure must NOT tear down the healthy DB.
        try:
            config = await self._configure_extras(
                ref=ref, endpoint=endpoint, token=token, service_role_key=service, spec=spec
            )
        except Exception as e:  # noqa: BLE001 - extras are additive; never fail the core
            logger.warning("supabase config extras failed for %s: %s", ref, e)
            config = {"errors": [str(e)]}

        secrets: dict[str, tuple[str, str]] = {}
        if db_url:
            secrets["database_url"] = (db_url, "db_url")
        if service:
            secrets["service_role_key"] = (service, "api_key")
        if anon:
            secrets["anon_key"] = (anon, "api_key")
        return ProvisionOutcome(
            external_id=ref, endpoint_url=endpoint, secrets=secrets,
            public={"anon_key": anon}, config=config,
        )

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        await self._safe_delete(settings.supabase_management_token, external_id)

    async def _safe_delete(self, token: str, ref: str) -> None:
        try:
            async with self._client() as client:
                await supa.delete_project(token, ref, client=client)
        except Exception:  # noqa: BLE001 - best-effort; a leaked project is logged for manual cleanup
            logger.warning("best-effort delete of Supabase project %s failed", ref, exc_info=True)

    async def _configure_extras(
        self, *, ref: str, endpoint: str, token: str, service_role_key: str | None, spec: dict
    ) -> dict:
        """Apply storage buckets (data-plane on the project host), auth config, and edge functions
        (Management API). Best-effort: each item is tried independently; failures collect in
        `errors` so a partial config still yields a usable handle."""
        report: dict = {"buckets": [], "functions": [], "auth": None, "errors": []}
        storage = spec.get("storage") or {}
        buckets = storage.get("buckets") or []
        auth = spec.get("auth")
        functions = spec.get("functions") or []

        if buckets and service_role_key:
            async with httpx.AsyncClient(base_url=endpoint, timeout=supa._TIMEOUT) as sclient:
                for b in buckets:
                    bname = b.get("name") if isinstance(b, dict) else str(b)
                    public = bool(b.get("public")) if isinstance(b, dict) else False
                    try:
                        await supa.create_storage_bucket(service_role_key, name=bname, public=public, client=sclient)
                        report["buckets"].append(bname)
                    except Exception as e:  # noqa: BLE001
                        report["errors"].append(f"bucket {bname}: {e}")

        if auth or functions:
            async with self._client() as mclient:
                if isinstance(auth, dict):
                    patch = {k: auth[k] for k in ("site_url", "external_email_enabled") if auth.get(k) is not None}
                    try:
                        if patch:
                            await supa.update_auth_config(token, ref, patch=patch, client=mclient)
                        report["auth"] = {"applied": sorted(patch.keys())}
                    except Exception as e:  # noqa: BLE001
                        report["errors"].append(f"auth: {e}")
                for fn in functions:
                    slug = fn.get("slug") if isinstance(fn, dict) else str(fn)
                    source = fn.get("source") if isinstance(fn, dict) else None
                    if not source:
                        report["errors"].append(f"function {slug}: skipped (no source provided)")
                        continue
                    try:
                        await supa.deploy_edge_function(token, ref, slug=slug, source=source, client=mclient)
                        report["functions"].append(slug)
                    except Exception as e:  # noqa: BLE001
                        report["errors"].append(f"function {slug}: {e}")

        return report
