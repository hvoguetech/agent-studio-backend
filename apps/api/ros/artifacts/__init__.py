"""Artifact storage (WS7). docs/design/artifact-storage.md."""

from ros.artifacts.base import ArtifactRef, ObjectStore, ObjectStoreError
from ros.artifacts.store import (
    ArtifactStore,
    BucketResolver,
    StoreTarget,
    get_artifact_store,
    reset_artifact_store,
)

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "BucketResolver",
    "ObjectStore",
    "ObjectStoreError",
    "StoreTarget",
    "get_artifact_store",
    "reset_artifact_store",
]
