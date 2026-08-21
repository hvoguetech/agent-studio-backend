"""build_compile_context wires ctx.workflow_id (the claude_code node's stable workspace keys on it).

The stable per-node workspace is <base>/<workflow_id>/<node_id>; without workflow_id the node falls
back to a throwaway temp dir, silently defeating clone-once + cross-run persistence. This locks the
choke point so a run/resume path can't quietly stop passing it (regression: it was originally set at
only one of six runs.py call sites).
"""

from __future__ import annotations

from ros.db.base import SessionLocal
from ros.models import Project, Workflow
from ros.services.runtime import build_compile_context


async def _project_and_workflow(s, t, p_slug):
    proj = Project(tenant_id=t, name="P", slug=p_slug, config={})
    s.add(proj)
    await s.flush()
    wf = Workflow(tenant_id=t, project_id=proj.id, name="f", executable={"nodes": [], "edges": []})
    s.add(wf)
    await s.flush()
    return proj, wf


async def test_workflow_id_carried_onto_ctx():
    t = "t_wfid"
    async with SessionLocal() as s:
        proj, wf = await _project_and_workflow(s, t, "p-wfid")
        ctx = await build_compile_context(s, tenant_id=t, project_id=proj.id, workflow_id=wf.id)
        assert ctx.workflow_id == wf.id


async def test_workflow_id_defaults_to_none_when_omitted():
    t = "t_wfid_none"
    async with SessionLocal() as s:
        proj, _ = await _project_and_workflow(s, t, "p-wfid-none")
        ctx = await build_compile_context(s, tenant_id=t, project_id=proj.id)
        assert ctx.workflow_id is None
