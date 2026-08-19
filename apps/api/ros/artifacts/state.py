"""The artifact PLANE: how artifacts ride graph state between nodes.

`store.py` owns bytes (key scheme, content-addressing, backends); this module owns the handoff —
turning an `ArtifactRef` into the plain dict a node writes to the `artifacts` state channel, and
back. The channel's reducer lives in `ros.engine.state` (`_merge_artifacts`, identity =
`(bucket, key)`), which is why entries here are dicts, not dataclasses: they are checkpointed.

Producer:  return {"artifacts": [await emit(..., data=b"...", filename="report.pdf")]}
Consumer:  data = await load(select(state, filename="report.pdf")[0])

Edge `mappings` can select refs with plain JMESPath over the channel — no new routing primitive:
    {"from": "artifacts[?produced_by=='builder'] | [0]", "to": "build_output"}

Invariant (docs/design/artifact-storage.md §3): refs in the checkpoint, bytes in the store. An
entry is deterministic — no timestamps, no ids — so a replayed emit reproduces it byte-for-byte
and the reducer becomes a no-op instead of appending a duplicate.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, fields
from typing import Any

from ros.artifacts.base import ArtifactRef, ObjectStoreError
from ros.artifacts.store import get_artifact_store

log = logging.getLogger("ros.artifacts")

# The default state channel every compiled workflow carries (ros.engine.state).
ARTIFACTS_KEY = "artifacts"

_REF_FIELDS = tuple(f.name for f in fields(ArtifactRef))


def to_entry(ref: ArtifactRef, *, produced_by: str | None = None) -> dict:
    """`ArtifactRef` -> the checkpoint-safe dict that rides the `artifacts` channel.

    `produced_by` (the emitting node id) is graph metadata, not part of the durable pointer, so
    it lives on the entry rather than on `ArtifactRef` — it is what downstream nodes filter on."""
    return {**asdict(ref), "produced_by": produced_by}


def from_entry(entry: Mapping[str, Any] | ArtifactRef) -> ArtifactRef:
    """Entry dict -> `ArtifactRef`, ignoring the plane's own metadata (`produced_by`)."""
    if isinstance(entry, ArtifactRef):
        return entry
    if not isinstance(entry, Mapping) or not entry.get("key"):
        raise ObjectStoreError(f"not an artifact entry (no key): {entry!r}")
    return ArtifactRef(**{k: entry[k] for k in _REF_FIELDS if k in entry})


async def emit(
    *,
    tenant_id: str,
    project_id: str,
    run_id: str,
    data: bytes,
    filename: str | None = None,
    content_type: str = "application/octet-stream",
    produced_by: str | None = None,
    store: Any = None,
) -> dict:
    """Store bytes and return the entry to write to `{"artifacts": [...]}`.

    Write-ahead by construction: the bytes land before the caller returns the entry (and so
    before the checkpoint that records it), and the key is content-addressed, so a crash between
    the two is repaired by the retry writing the same key."""
    ref = await (store or get_artifact_store()).put(
        tenant_id=tenant_id, project_id=project_id, run_id=run_id,
        data=data, filename=filename, content_type=content_type,
    )
    return to_entry(ref, produced_by=produced_by)


async def load(entry: Mapping[str, Any] | ArtifactRef, *, store: Any = None) -> bytes:
    """Fetch an artifact's bytes. The bytes stay in the node — never write them back to state."""
    return await (store or get_artifact_store()).get(from_entry(entry))


def select(
    state: Mapping[str, Any] | None,
    *,
    produced_by: str | None = None,
    filename: str | None = None,
    content_type: str | None = None,
) -> list[dict]:
    """The `artifacts` entries in `state` matching every filter given, in emission order.

    The Python-side counterpart to a JMESPath edge mapping, for node code that consumes
    artifacts directly. No filters = every artifact in the run so far."""
    entries = (state or {}).get(ARTIFACTS_KEY) or []
    if not isinstance(entries, list):
        entries = [entries]
    out = []
    for e in entries:
        if not isinstance(e, Mapping):
            continue
        if produced_by is not None and e.get("produced_by") != produced_by:
            continue
        if filename is not None and e.get("filename") != filename:
            continue
        if content_type is not None and e.get("content_type") != content_type:
            continue
        out.append(dict(e))
    return out


def run_scope(config: Mapping[str, Any] | None) -> str:
    """The run id to key artifacts under, from a LangGraph invocation config.

    Runs carry `configurable.run_id` (ros.services.runs); the thread id is the fallback for
    entrypoints that drive a graph without a `Run` row, and `adhoc` matches what the artifact
    router uses for uploads outside any run."""
    configurable = (config or {}).get("configurable") or {}
    return configurable.get("run_id") or configurable.get("thread_id") or "adhoc"
