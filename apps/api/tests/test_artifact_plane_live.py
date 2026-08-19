"""LIVE-VERIFY for the artifact plane against a REAL S3-compatible bucket.

Skipped unless the s3 backend is configured, so CI stays infra-free. The rest of the plane
(channel, reducer, entry bridge, checkpoint discipline) is backend-independent and covered by
`test_artifact_plane.py`; this file covers only what a fake cannot: boto3 request shaping,
SigV4 signing, virtual-host vs path addressing, and the key scheme as the bucket really stores it.

Run it with the provisioned bucket's own credentials — note `ROS_ARTIFACT_BUCKET` must be the
PROVISIONED bucket name (Railway's display name differs from it):

    ROS_ARTIFACT_STORE=s3 \\
    ROS_ARTIFACT_BUCKET=$(railway bucket credentials --bucket <name> --json | jq -r .bucketName) \\
    ROS_S3_ENDPOINT_URL=... ROS_S3_REGION=auto ROS_S3_ADDRESSING_STYLE=virtual \\
    ROS_S3_ACCESS_KEY_ID=... ROS_S3_SECRET_ACCESS_KEY=... \\
    .venv312/bin/python -m pytest tests/test_artifact_plane_live.py -q
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ros.artifacts import get_artifact_store, reset_artifact_store
from ros.artifacts.state import ARTIFACTS_KEY, emit, load, select
from ros.config import settings
from ros.engine.state import build_state_typeddict

pytestmark = pytest.mark.skipif(
    (os.environ.get("ROS_ARTIFACT_STORE") or "").lower() != "s3"
    or not os.environ.get("ROS_S3_ACCESS_KEY_ID"),
    reason="live S3 not configured (ROS_ARTIFACT_STORE=s3 + ROS_S3_* creds)",
)

TENANT, PROJECT = "live-t", "live-p"
PAYLOAD = b"PAYLOAD-FROM-writer"


@pytest.fixture
def run_id() -> str:
    """A fresh run prefix per test, so a repeat run can never collide with a stale object."""
    return f"live-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _live_store():
    reset_artifact_store()
    yield
    reset_artifact_store()


def _graph(run_id: str):
    schema = build_state_typeddict({"seen": {"type": "str", "reducer": "last"}})
    g = StateGraph(schema)

    async def writer(state):
        entry = await emit(tenant_id=TENANT, project_id=PROJECT, run_id=run_id, data=PAYLOAD,
                           filename="writer.txt", content_type="text/plain", produced_by="writer")
        return {ARTIFACTS_KEY: [entry]}

    async def reader(state):
        data = await load(select(state, produced_by="writer")[0])
        return {"seen": f"len={len(data)}"}  # read the bytes; do NOT put them back in state

    g.add_node("writer", writer)
    g.add_node("reader", reader)
    g.add_edge(START, "writer")
    g.add_edge("writer", "reader")
    g.add_edge("reader", END)
    return g


async def test_backend_is_really_s3():
    assert get_artifact_store().backend.name == "s3"


async def test_handoff_through_a_real_bucket(run_id):
    """Producer -> bucket -> consumer, end to end, with the object read back off the wire."""
    saver = InMemorySaver()
    graph = _graph(run_id).compile(checkpointer=saver)
    config = {"configurable": {"thread_id": f"th-{run_id}", "run_id": run_id}}
    store = get_artifact_store()

    out = await graph.ainvoke({}, config)
    [entry] = out[ARTIFACTS_KEY]
    try:
        assert out["seen"] == f"len={len(PAYLOAD)}"
        assert entry["bucket"] == settings.artifact_bucket
        env = (settings.environment or "dev").strip().lower()
        assert entry["key"] == f"{env}/{TENANT}/{PROJECT}/{run_id}/{entry['sha256']}/writer.txt"
        # ...and the object is genuinely in the bucket, not just in our state.
        assert await store.backend.get_bytes(entry["bucket"], entry["key"]) == PAYLOAD
        assert "PAYLOAD-FROM-writer" not in repr((await saver.aget_tuple(config)).checkpoint)
    finally:
        await store.delete_run(TENANT, PROJECT, run_id)


async def test_presigned_url_downloads(run_id):
    """The signed URL must work from an unauthenticated client — this is what catches a wrong
    addressing style or region, which a fake backend cannot."""
    store = get_artifact_store()
    entry = await emit(tenant_id=TENANT, project_id=PROJECT, run_id=run_id, data=PAYLOAD,
                       filename="report.txt", content_type="text/plain")
    try:
        from ros.artifacts.state import from_entry

        url = await store.presign(from_entry(entry), expires_s=300)
        assert url.startswith("http")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
        assert resp.status_code == 200, resp.text[:300]
        assert resp.content == PAYLOAD
        assert "attachment" in resp.headers.get("content-disposition", "")
    finally:
        await store.delete_run(TENANT, PROJECT, run_id)


async def test_content_addressing_and_delete_run(run_id):
    """Re-emitting identical bytes must overwrite one key (the resume-safety invariant), and a
    run's objects must be reclaimable by prefix."""
    store = get_artifact_store()
    a = await emit(tenant_id=TENANT, project_id=PROJECT, run_id=run_id, data=PAYLOAD, filename="a.txt")
    b = await emit(tenant_id=TENANT, project_id=PROJECT, run_id=run_id, data=PAYLOAD, filename="a.txt")
    assert a == b
    await emit(tenant_id=TENANT, project_id=PROJECT, run_id=run_id, data=b"other", filename="b.txt")
    assert await store.delete_run(TENANT, PROJECT, run_id) == 2  # 2 distinct keys, not 3 writes
