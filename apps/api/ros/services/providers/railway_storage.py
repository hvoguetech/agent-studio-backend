"""Railway storage provider — an S3-compatible object-storage bucket per agent (Railway-only stack).

Provisions an isolated Railway project + a bucket, and returns its S3 credentials as secret refs
(access key + secret key) plus the endpoint + bucket name. Completes the single-vendor stack
(DB=railway-postgres, compute=railway, queue=queue, storage=here). Reuses the Railway GraphQL client.

⚠️ LIVE-VERIFY: Railway does not publish the bucket GraphQL mutations in its LLM docs (only the
`railway bucket` CLI). `bucketCreate` / bucket-credentials shapes are best-known and MUST be verified
against a live token — isolated to this module; fake-transport tested only.

⚠️ CONFIRMED DEFECT (2026-08-19, against a live Railway bucket): the S3 bucket name is NOT the
bucket's display `name`. `railway bucket list` reports `ros-artifacts` while the addressable bucket
is `ros-artifacts-eaduobyfsc`; a PutObject to the display name fails with `NoSuchBucket`. So the
`bucket` recorded in `public`/`config` below is UNUSABLE by an agent as an S3 bucket. The CLI's
`bucket credentials` payload carries the real one as `bucketName`, plus `region` and `urlStyle`
(`virtual-host` → ROS_S3_ADDRESSING_STYLE=virtual), none of which `_BUCKET_CREDS` selects. Fixing it
means adding those fields to the query — which needs a live `RAILWAY_API_TOKEN` to confirm the
GraphQL field names first, since a wrong field name makes the whole query error out.
"""

from __future__ import annotations

import logging

import httpx

from ros.config import settings
from ros.services.providers.base import ProvisionError, ProvisionOutcome
from ros.services.providers.railway import (
    RAILWAY_API,
    _PROJECT_CREATE,
    _PROJECT_DELETE,
    _TIMEOUT,
    _gql,
    _prune,
)

logger = logging.getLogger(__name__)

_BUCKET_CREATE = """
mutation($input: BucketCreateInput!) { bucketCreate(input: $input) { id name } }"""
_BUCKET_CREDS = """
query($id: String!) { bucket(id: $id) { endpoint accessKeyId secretAccessKey } }"""


class RailwayStorageProvider:
    kind = "railway-storage"

    def is_enabled(self) -> bool:
        return bool(settings.railway_api_token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=RAILWAY_API, timeout=_TIMEOUT)

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        token = settings.railway_api_token
        bucket_name = spec.get("bucket") or name
        async with self._client() as client:
            proj = (await _gql(token, _PROJECT_CREATE,
                               {"input": _prune({"name": f"{name}-storage", "workspaceId": settings.railway_workspace_id})},
                               client=client)).get("projectCreate") or {}
            project_id = proj.get("id")
            if not project_id:
                raise ProvisionError(f"railway projectCreate returned no id: {proj!r}")
            envs = [e["node"] for e in (proj.get("environments") or {}).get("edges", [])]
            env_id = next((e["id"] for e in envs if (e.get("name") or "").lower() == "production"),
                          envs[0]["id"] if envs else None)
            try:
                bucket = (await _gql(token, _BUCKET_CREATE, {"input": _prune({
                    "projectId": project_id, "environmentId": env_id, "name": bucket_name,
                })}, client=client)).get("bucketCreate") or {}
                bucket_id = bucket.get("id")
                if not bucket_id:
                    raise ProvisionError(f"bucketCreate returned no id: {bucket!r}")
                creds = (await _gql(token, _BUCKET_CREDS, {"id": bucket_id}, client=client)).get("bucket") or {}
            except Exception as e:
                await self._safe_delete(token, project_id)
                raise ProvisionError(f"railway-storage provisioning failed after project create ({project_id}): {e}") from e

        endpoint = creds.get("endpoint")
        secrets: dict[str, tuple[str, str]] = {}
        if creds.get("accessKeyId"):
            secrets["s3_access_key_id"] = (creds["accessKeyId"], "api_key")
        if creds.get("secretAccessKey"):
            secrets["s3_secret_access_key"] = (creds["secretAccessKey"], "api_key")
        return ProvisionOutcome(
            external_id=project_id, endpoint_url=endpoint, secrets=secrets,
            public=_prune({"bucket": bucket.get("name") or bucket_name, "bucket_id": bucket_id, "endpoint": endpoint}),
            config=_prune({"environment_id": env_id, "bucket_id": bucket_id, "bucket": bucket_name}),
        )

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        await self._safe_delete(settings.railway_api_token, external_id)

    async def _safe_delete(self, token: str, project_id: str) -> None:
        try:
            async with self._client() as client:
                await _gql(token, _PROJECT_DELETE, {"id": project_id}, client=client)
        except Exception:  # noqa: BLE001 - best-effort; a leaked project is logged for manual cleanup
            logger.warning("best-effort delete of Railway storage project %s failed", project_id, exc_info=True)
