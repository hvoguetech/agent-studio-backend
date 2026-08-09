"""Artifact storage seam (WS7). See docs/design/artifact-storage.md.

`ObjectStore` is the raw backend (put/get/delete/presign by bucket+key); `ArtifactStore`
(store.py) is the backend-agnostic high level that owns the key scheme, content-addressing, and
the `BucketResolver`. Bytes live in the store; only an `ArtifactRef` goes into run state / the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ObjectStoreError(RuntimeError):
    """Store-infrastructure failure (misconfig, transport, missing object)."""


@dataclass(frozen=True)
class ArtifactRef:
    """Durable pointer persisted in run state / the DB. Never carries the bytes."""

    bucket: str
    key: str
    sha256: str
    size: int
    content_type: str = "application/octet-stream"
    filename: str | None = None


@runtime_checkable
class ObjectStore(Protocol):
    name: str

    async def put_bytes(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None: ...

    async def get_bytes(self, bucket: str, key: str) -> bytes: ...

    async def delete_prefix(self, bucket: str, prefix: str) -> int: ...

    async def presign_get(
        self, bucket: str, key: str, *, expires_s: int = 900, filename: str | None = None
    ) -> str: ...
