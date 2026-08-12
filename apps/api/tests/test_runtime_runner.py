"""Standalone runner (Part C) — compile + run a workflow from a manifest, offline (fake model), and
the in-memory secret source (manifest carries resolved refs; runtime resolves them without the DB)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from ros.db.base import SessionLocal
from ros.models import Project, Tool
from ros.runtime.runner import build_graph, run
from ros.runtime.secret_source import InMemorySecretStore
from ros.services.runtime_manifest import MANIFEST_FORMAT, RuntimeManifestService
from ros.services.secrets import SecretService

_FAKE_WF = {
    "id": "w", "version": 1,
    "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
    "entry_node": "start",
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {"id": "agent", "type": "agent", "config": {"flavor": "agent", "model": "fake:Hello from the runtime."}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [
        {"source": "start", "target": "agent"},
        {"source": "agent", "target": "end"},
    ],
}


def _manifest(executable: dict, secrets: dict | None = None) -> dict:
    return {
        "format": MANIFEST_FORMAT, "tenant_id": "t", "project_id": "p", "workflow_id": "w",
        "executable": executable, "default_model": "fake:hi", "default_middleware": [], "egress": None,
        "provider_credentials": {}, "tools": [], "toolset_members": {}, "components": [],
        "mcp_clients": [], "agent_presets": {}, "workflows": {}, "secrets": secrets or {},
    }


async def test_runner_compiles_and_runs_from_manifest():
    result = await run(_manifest(_FAKE_WF), {"messages": [HumanMessage(content="hi")]}, thread_id="r1")
    last = result["messages"][-1]
    assert isinstance(last, AIMessage)
    assert "Hello from the runtime." in last.content


def test_build_graph_from_manifest_is_compilable():
    graph = build_graph(_manifest(_FAKE_WF))
    assert graph is not None  # manifest -> CompileContext -> compiled graph, no DB


async def test_in_memory_secret_source_resolves_from_manifest():
    store = InMemorySecretStore({"secret://proj/api_key": "sk-123"})
    got = await store.read_ref(tenant_id="t", project_id="p", ref="secret://proj/api_key")
    assert got == "sk-123"


async def test_manifest_carries_resolved_secret_for_a_referencing_tool():
    async with SessionLocal() as s:
        proj = Project(tenant_id="t_sec_m", name="sm", slug="sm-slug", config={})
        s.add(proj)
        await s.flush()
        pid = proj.id
        await SecretService.write(s, "t_sec_m", pid, name="api_key", value="sk-xyz", kind="api_key")
        # A tool whose config references the secret ref (a builtin ignores the extra field).
        s.add(Tool(tenant_id="t_sec_m", project_id=pid, name="clock", kind="builtin",
                   config={"builtin": "current_time", "note": "secret://proj/api_key"}, enabled=True))
        # A workflow to build the manifest for.
        from ros.models import Workflow
        wf = Workflow(tenant_id="t_sec_m", project_id=pid, name="f", executable={"nodes": [], "edges": []})
        s.add(wf)
        await s.flush()
        wid = wf.id
        await s.commit()
        manifest = await RuntimeManifestService.build(s, tenant_id="t_sec_m", project_id=pid, workflow_id=wid)

    assert manifest["secrets"]["secret://proj/api_key"] == "sk-xyz"
    # The runtime resolves it via the in-memory store, no DB.
    store = InMemorySecretStore(manifest["secrets"])
    assert await store.read_ref(tenant_id="t_sec_m", project_id="p", ref="secret://proj/api_key") == "sk-xyz"
