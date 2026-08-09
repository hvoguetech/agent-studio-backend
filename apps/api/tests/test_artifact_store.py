"""WS7 artifact storage: ArtifactStore key scheme + content-addressing + resolver, the local
backend round-trip, size cap, registry, and the S3 backend's request shaping (mocked boto3)."""

from __future__ import annotations

import sys

import pytest

from ros.artifacts import ArtifactStore, BucketResolver, ObjectStoreError
from ros.artifacts.backends import LocalObjectStore
from ros.artifacts.store import _build_backend, get_artifact_store, reset_artifact_store
from ros.config import settings


def _store(tmp_path) -> ArtifactStore:
    return ArtifactStore(LocalObjectStore(str(tmp_path)))


# --- key scheme / content-addressing / isolation ---
async def test_local_round_trip_and_key_scheme(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    st = _store(tmp_path)
    ref = await st.put(tenant_id="t1", project_id="p1", run_id="r1", data=b"hello",
                       filename="report.txt", content_type="text/plain")
    # key: {env}/{tenant}/{project}/{run}/{sha}/{filename}
    assert ref.key == f"production/t1/p1/r1/{ref.sha256}/report.txt"
    assert ref.size == 5 and ref.content_type == "text/plain"
    assert await st.get(ref) == b"hello"


async def test_content_addressed_is_idempotent(tmp_path):
    st = _store(tmp_path)
    a = await st.put(tenant_id="t", project_id="p", run_id="r", data=b"same")
    b = await st.put(tenant_id="t", project_id="p", run_id="r", data=b"same")
    c = await st.put(tenant_id="t", project_id="p", run_id="r", data=b"different")
    assert a.key == b.key            # same bytes -> same key (resume/retry-safe, no dupes)
    assert a.sha256 != c.sha256 and a.key != c.key


async def test_filename_is_sanitized_no_traversal(tmp_path):
    st = _store(tmp_path)
    ref = await st.put(tenant_id="t", project_id="p", run_id="r", data=b"x",
                       filename="../../etc/passwd")
    assert ".." not in ref.key and "/etc/" not in ref.key
    assert await st.get(ref) == b"x"  # still stored + retrievable under the safe key


async def test_delete_run_and_project(tmp_path):
    st = _store(tmp_path)
    await st.put(tenant_id="t", project_id="p", run_id="r1", data=b"a")
    await st.put(tenant_id="t", project_id="p", run_id="r2", data=b"b")
    assert await st.delete_run("t", "p", "r1") == 1
    assert await st.delete_project("t", "p") == 1  # r2 remains -> 1


async def test_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "artifact_max_bytes", 4)
    st = _store(tmp_path)
    with pytest.raises(ObjectStoreError, match="exceeds"):
        await st.put(tenant_id="t", project_id="p", run_id="r", data=b"toolong")


# --- resolver (bucket management) ---
def test_default_resolver_shared_bucket(monkeypatch):
    monkeypatch.setattr(settings, "artifact_bucket", "shared-bkt")
    monkeypatch.setattr(settings, "environment", "prod")
    t = BucketResolver().resolve("tenantA", "projX")
    assert t.bucket == "shared-bkt" and t.prefix == "prod/tenantA/projX"


async def test_enterprise_resolver_override(tmp_path):
    class Dedicated(BucketResolver):
        def resolve(self, tenant_id, project_id):
            from ros.artifacts.store import StoreTarget
            return StoreTarget(bucket=f"cust-{tenant_id}", prefix=f"{tenant_id}/{project_id}")

    st = ArtifactStore(LocalObjectStore(str(tmp_path)), resolver=Dedicated())
    ref = await st.put(tenant_id="big", project_id="p", run_id="r", data=b"x")
    assert ref.bucket == "cust-big" and ref.key.startswith("big/p/r/")


# --- registry ---
def test_registry_default_is_local(monkeypatch):
    monkeypatch.setattr(settings, "artifact_store", "local")
    reset_artifact_store()
    assert get_artifact_store().backend.name == "local"
    reset_artifact_store()


def test_registry_unknown_backend(monkeypatch):
    monkeypatch.setattr(settings, "artifact_store", "nope")
    with pytest.raises(ObjectStoreError, match="unknown ROS_ARTIFACT_STORE"):
        _build_backend()


# --- s3 backend request shaping (mocked boto3) ---
class _FakeS3:
    def __init__(self):
        self.objs, self.calls = {}, []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.calls.append(("put", Bucket, Key, ContentType))
        self.objs[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}

    def get_paginator(self, _op):
        objs = self.objs
        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for (b, k) in objs if b == Bucket and k.startswith(Prefix)]}
        return _P()

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objs.pop((Bucket, o["Key"]), None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append(("presign", Params.get("ResponseContentDisposition"), ExpiresIn))
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?sig=1&e={ExpiresIn}"


def _install_fake_boto3(monkeypatch):
    fake = _FakeS3()
    import types
    mod = types.ModuleType("boto3")
    mod.client = lambda *a, **k: fake
    monkeypatch.setitem(sys.modules, "boto3", mod)
    return fake


async def test_s3_put_get_presign_delete(monkeypatch):
    fake = _install_fake_boto3(monkeypatch)
    from ros.artifacts.backends import S3ObjectStore
    s3 = S3ObjectStore(endpoint_url="https://x", region="auto", access_key_id="k", secret_access_key="s")
    await s3.put_bytes("bkt", "a/b/c.txt", b"data", content_type="text/plain")
    assert ("put", "bkt", "a/b/c.txt", "text/plain") in fake.calls
    assert await s3.get_bytes("bkt", "a/b/c.txt") == b"data"
    url = await s3.presign_get("bkt", "a/b/c.txt", expires_s=600, filename="c.txt")
    assert "s3.example/bkt/a/b/c.txt" in url and ("presign", 'attachment; filename="c.txt"', 600) in fake.calls
    assert await s3.delete_prefix("bkt", "a/") == 1
