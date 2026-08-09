"""High-level artifact API + bucket management. `ArtifactStore` owns the server-generated key
scheme (tenant/project/run/sha), content-addressing (idempotent writes), and the size cap, so
isolation and crash-safety don't depend on the backend or the caller. `BucketResolver` maps a
(tenant, project) to a (bucket, key-prefix) — shared-bucket by default; subclass for enterprise
dedicated/BYO buckets."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ros.artifacts.base import ArtifactRef, ObjectStore, ObjectStoreError
from ros.config import settings

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str | None) -> str:
    if not name:
        return "file"
    cleaned = _UNSAFE.sub("_", name).strip("._") or "file"
    return cleaned[-128:]


@dataclass(frozen=True)
class StoreTarget:
    bucket: str
    prefix: str  # e.g. "production/<tenant>/<project>"


class BucketResolver:
    """Default: ONE shared bucket, isolated by a `{env}/{tenant}/{project}` key prefix. Enterprise
    dedicated/BYO/region buckets = subclass and override `resolve` (the pluggable seam)."""

    def resolve(self, tenant_id: str, project_id: str) -> StoreTarget:
        env = (settings.environment or "dev").strip().lower()
        return StoreTarget(bucket=settings.artifact_bucket, prefix=f"{env}/{tenant_id}/{project_id}")


class ArtifactStore:
    def __init__(self, backend: ObjectStore, resolver: BucketResolver | None = None) -> None:
        self.backend = backend
        self.resolver = resolver or BucketResolver()

    @staticmethod
    def _key(target: StoreTarget, run_id: str, sha256: str, filename: str | None) -> str:
        # Content-addressed: same bytes -> same key -> idempotent re-write on a resume/retry.
        return f"{target.prefix}/{run_id}/{sha256}/{_safe_filename(filename)}"

    async def put(
        self,
        *,
        tenant_id: str,
        project_id: str,
        run_id: str,
        data: bytes,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        """Store bytes and return a durable `ArtifactRef` (put THIS in run state / the DB, not the
        bytes). Write-ahead: bytes land before the caller records the ref."""
        cap = settings.artifact_max_bytes
        if cap and len(data) > cap:
            raise ObjectStoreError(f"artifact is {len(data)} bytes; exceeds ROS_ARTIFACT_MAX_BYTES={cap}")
        target = self.resolver.resolve(tenant_id, project_id)
        sha256 = hashlib.sha256(data).hexdigest()
        key = self._key(target, run_id, sha256, filename)
        await self.backend.put_bytes(target.bucket, key, data, content_type=content_type)
        return ArtifactRef(
            bucket=target.bucket, key=key, sha256=sha256, size=len(data),
            content_type=content_type, filename=filename,
        )

    async def get(self, ref: ArtifactRef) -> bytes:
        return await self.backend.get_bytes(ref.bucket, ref.key)

    async def presign(self, ref: ArtifactRef, *, expires_s: int = 900) -> str:
        return await self.backend.presign_get(
            ref.bucket, ref.key, expires_s=expires_s, filename=ref.filename
        )

    async def delete_run(self, tenant_id: str, project_id: str, run_id: str) -> int:
        t = self.resolver.resolve(tenant_id, project_id)
        return await self.backend.delete_prefix(t.bucket, f"{t.prefix}/{run_id}/")

    async def delete_project(self, tenant_id: str, project_id: str) -> int:
        t = self.resolver.resolve(tenant_id, project_id)
        return await self.backend.delete_prefix(t.bucket, f"{t.prefix}/")


_store: ArtifactStore | None = None


def _build_backend() -> ObjectStore:
    key = (settings.artifact_store or "local").strip().lower()
    if key == "local":
        from ros.artifacts.backends import LocalObjectStore

        return LocalObjectStore(settings.artifact_local_dir)
    if key == "s3":
        from ros.artifacts.backends import S3ObjectStore

        return S3ObjectStore(
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    raise ObjectStoreError(f"unknown ROS_ARTIFACT_STORE={key!r} (expected 'local' or 's3')")


def get_artifact_store() -> ArtifactStore:
    """Process-wide artifact store (backend selected by ROS_ARTIFACT_STORE, resolved once)."""
    global _store
    if _store is None:
        _store = ArtifactStore(_build_backend())
    return _store


def reset_artifact_store() -> None:
    global _store
    _store = None
