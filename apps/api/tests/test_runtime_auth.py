"""Scoped run token + run-token-gated manifest endpoint + the runner's prod checkpointer selection."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ros.config import settings
from ros.db.base import SessionLocal
from ros.models import Project, Run, Workflow
from ros.routers.runtime import get_run_manifest
from ros.runtime.runner import _durable_checkpointer
from ros.security import TokenError, create_access_token, create_run_token, decode_token
from ros.services.runtime_manifest import MANIFEST_FORMAT


def _req(token: str) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""})


def test_run_token_round_trip():
    claims = decode_token(create_run_token(run_id="r1", tenant_id="t1", project_id="p1"), expected_type="run")
    assert claims["sub"] == "r1" and claims["tid"] == "t1" and claims["scope"] == "runtime:pull"


def test_non_run_token_rejected_as_run():
    tok = create_access_token(user_id="u", tenant_id="t", role="admin")
    with pytest.raises(TokenError):
        decode_token(tok, expected_type="run")


async def test_manifest_endpoint_returns_manifest_for_valid_run_token():
    async with SessionLocal() as s:
        proj = Project(tenant_id="t_rt", name="p", slug="p-rt", config={})
        s.add(proj)
        await s.flush()
        wf = Workflow(tenant_id="t_rt", project_id=proj.id, name="f", executable={"nodes": [], "edges": []})
        s.add(wf)
        await s.flush()
        run = Run(tenant_id="t_rt", project_id=proj.id, workflow_id=wf.id, thread_id="th1", status="queued")
        s.add(run)
        await s.flush()
        rid, wid = run.id, wf.id
        await s.commit()

        tok = create_run_token(run_id=rid, tenant_id="t_rt", project_id=proj.id)
        manifest = await get_run_manifest(rid, _req(tok), session=s)
    assert manifest["format"] == MANIFEST_FORMAT
    assert manifest["workflow_id"] == wid


async def test_manifest_endpoint_rejects_bad_token():
    async with SessionLocal() as s:
        with pytest.raises(HTTPException) as ei:
            await get_run_manifest("r_x", _req("garbage"), session=s)
    assert ei.value.status_code == 401


async def test_manifest_endpoint_rejects_token_for_a_different_run():
    async with SessionLocal() as s:
        tok = create_run_token(run_id="r_a", tenant_id="t", project_id="p")
        with pytest.raises(HTTPException) as ei:
            await get_run_manifest("r_b", _req(tok), session=s)  # sub != path run_id
    assert ei.value.status_code == 403


async def test_runner_checkpointer_is_in_memory_offline(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    monkeypatch.setattr(settings, "checkpoint_backend", "sqlite")
    async with _durable_checkpointer() as cp:
        assert isinstance(cp, InMemorySaver)  # non-postgres -> in-process (prod uses postgres)
