"""ObjectStore backends: `local` (filesystem/volume, default, zero-infra) and `s3` (S3-compatible;
Railway bucket / R2 / MinIO / AWS). boto3 is imported lazily so the core needs it only when
`ROS_ARTIFACT_STORE=s3` (install the `[storage]` extra)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ros.artifacts.base import ObjectStoreError


class LocalObjectStore:
    """Bytes on the local filesystem under `<base>/<bucket>/<key>`. Single-node/dev; presign returns
    an internal marker (the Phase-2 artifact router serves it) since there's no signing service."""

    name = "local"

    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir)

    def _path(self, bucket: str, key: str) -> Path:
        # Keys are server-generated (no traversal), but resolve+guard anyway.
        p = (self._base / bucket / key).resolve()
        root = (self._base / bucket).resolve()
        if not str(p).startswith(str(root)):
            raise ObjectStoreError("resolved path escapes the bucket root")
        return p

    async def put_bytes(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        def _write() -> None:
            p = self._path(bucket, key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

        await asyncio.to_thread(_write)

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        def _read() -> bytes:
            try:
                return self._path(bucket, key).read_bytes()
            except FileNotFoundError as e:
                raise ObjectStoreError(f"object not found: {bucket}/{key}") from e

        return await asyncio.to_thread(_read)

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        def _delete() -> int:
            root = self._path(bucket, prefix)
            n = 0
            if root.exists():
                for f in root.rglob("*"):
                    if f.is_file():
                        f.unlink(missing_ok=True)
                        n += 1
            return n

        return await asyncio.to_thread(_delete)

    async def presign_get(
        self, bucket: str, key: str, *, expires_s: int = 900, filename: str | None = None
    ) -> str:
        # No signing service locally; the Phase-2 router serves this via an authorized route.
        return f"local://{bucket}/{key}"


class S3ObjectStore:
    """S3-compatible backend (boto3 + `endpoint_url` works for Railway/R2/MinIO/AWS). boto3 is sync,
    so calls run in a thread; presigned GET URLs are generated with a download disposition."""

    name = "s3"

    def __init__(self, *, endpoint_url, region, access_key_id, secret_access_key) -> None:
        try:
            import boto3  # noqa: F401
        except Exception as e:  # noqa: BLE001 - [storage] extra not installed
            raise ObjectStoreError(
                "ROS_ARTIFACT_STORE=s3 needs boto3 (install the '[storage]' extra)"
            ) from e
        import boto3

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
        )

    async def put_bytes(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        def _get() -> bytes:
            try:
                resp = self._client.get_object(Bucket=bucket, Key=key)
                return resp["Body"].read()
            except Exception as e:  # noqa: BLE001 - includes NoSuchKey
                raise ObjectStoreError(f"s3 get failed for {bucket}/{key}: {e}") from e

        return await asyncio.to_thread(_get)

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        def _delete() -> int:
            paginator = self._client.get_paginator("list_objects_v2")
            n = 0
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs:
                    self._client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                    n += len(objs)
            return n

        return await asyncio.to_thread(_delete)

    async def presign_get(
        self, bucket: str, key: str, *, expires_s: int = 900, filename: str | None = None
    ) -> str:
        params = {"Bucket": bucket, "Key": key}
        if filename:  # force a download rather than inline render (untrusted content)
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return await asyncio.to_thread(
            self._client.generate_presigned_url, "get_object", Params=params, ExpiresIn=expires_s
        )
