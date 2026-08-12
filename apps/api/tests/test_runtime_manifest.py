"""RunManifest (Part A) + manifest-sourced CompileContext (Part B).

Proves master serializes a run's definitions into a manifest, and the runtime rebuilds an EQUIVALENT
CompileContext from it (same tool registry / agent presets / workflows / default model) — the
control-plane/data-plane split, minus the DB on the runtime side."""

from __future__ import annotations

import pytest

from ros.db.base import SessionLocal
from ros.models import Agent, Project, Tool, Workflow
from ros.services.runtime import build_compile_context, build_compile_context_from_manifest
from ros.services.runtime_manifest import MANIFEST_FORMAT, RuntimeManifestService


async def _seed(tenant: str) -> tuple[str, str]:
    async with SessionLocal() as s:
        proj = Project(tenant_id=tenant, name="rm", slug=f"{tenant}-slug",
                       config={"budgets": {"max_usd_per_run": 2.0}, "model_aliases": {"gpt": "fake:aliased"}})
        s.add(proj)
        await s.flush()
        pid = proj.id
        s.add(Tool(tenant_id=tenant, project_id=pid, name="clock", kind="builtin",
                   config={"builtin": "current_time"}, enabled=True))
        s.add(Agent(tenant_id=tenant, project_id=pid, name="researcher", config={"model": "fake:1"}))
        wf = Workflow(tenant_id=tenant, project_id=pid, name="flow",
                      executable={"nodes": [], "edges": []})
        s.add(wf)
        await s.flush()
        wid = wf.id
        await s.commit()
    return pid, wid


async def test_build_manifest_contains_definitions():
    pid, wid = await _seed("t_rm1")
    async with SessionLocal() as s:
        manifest = await RuntimeManifestService.build(s, tenant_id="t_rm1", project_id=pid, workflow_id=wid)
    assert manifest["format"] == MANIFEST_FORMAT
    assert manifest["workflow_id"] == wid
    assert manifest["executable"] == {"nodes": [], "edges": []}
    clock = next(t for t in manifest["tools"] if t["name"] == "clock")
    assert clock["kind"] == "builtin" and clock["config"]["builtin"] == "current_time"
    assert any(p.get("model") == "fake:1" for p in manifest["agent_presets"].values())
    assert wid in manifest["workflows"]


async def test_manifest_context_parity_with_db_context():
    pid, wid = await _seed("t_rm2")
    async with SessionLocal() as s:
        manifest = await RuntimeManifestService.build(s, tenant_id="t_rm2", project_id=pid, workflow_id=wid)
        ctx_db = await build_compile_context(s, tenant_id="t_rm2", project_id=pid)

    ctx_m = build_compile_context_from_manifest(manifest)  # sync, no DB — the runtime path
    assert set(ctx_db.tool_registry.keys()) == set(ctx_m.tool_registry.keys())
    assert {} != ctx_m.tool_registry  # the builtin tool actually materialized from the manifest
    assert ctx_db.agent_presets == ctx_m.agent_presets
    assert set(ctx_db.workflows.keys()) == set(ctx_m.workflows.keys())
    assert ctx_db.default_model == ctx_m.default_model
    # WS9 drift carried through the manifest: model aliases + the tenant_budget hard-cap middleware.
    assert ctx_db.model_aliases == ctx_m.model_aliases == {"gpt": "fake:aliased"}
    assert ctx_db.project_default_mw == ctx_m.project_default_mw
    assert any(isinstance(e, dict) and e.get("type") == "tenant_budget" for e in ctx_m.project_default_mw)


async def test_build_manifest_404_for_missing_workflow():
    async with SessionLocal() as s:
        proj = Project(tenant_id="t_rm3", name="rm3", slug="t_rm3-slug", config={})
        s.add(proj)
        await s.commit()
        with pytest.raises(LookupError):
            await RuntimeManifestService.build(s, tenant_id="t_rm3", project_id=proj.id, workflow_id="nope")
