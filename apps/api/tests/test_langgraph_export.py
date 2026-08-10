"""WS8 follow-up: export a workflow as a runnable LangGraph Studio project."""

from __future__ import annotations

import io
import json
import uuid
import zipfile

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from ros.engine.compiler import compile_workflow
from ros.engine.context import CompileContext
from ros.main import create_app
from ros.security import create_access_token
from ros.services.auth import AuthService
from ros.services.langgraph_export import _graph_key, build_files, build_zip

_EXECUTABLE = {
    "id": "wf_demo", "version": 1,
    "state": {
        "messages": {"type": "list[message]", "reducer": "add_messages"},
        "payload": {"type": "json", "reducer": "last"},
        "data": {"type": "json", "reducer": "last"},
    },
    "entry_node": "start",
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {"id": "xf", "type": "transform", "config": {"expression": "payload", "output_key": "data"}},
        {"id": "end", "type": "end", "config": {}},
    ],
    "edges": [{"source": "start", "target": "xf"}, {"source": "xf", "target": "end"}],
}


def test_build_files_has_expected_bundle():
    files = build_files("wf_demo", "My Flow!", _EXECUTABLE)
    assert set(files) == {"langgraph.json", "graph.py", "executable.json", "requirements.txt", ".env.example", "README.md"}
    lg = json.loads(files["langgraph.json"])
    assert lg["env"] == ".env"
    assert lg["graphs"] == {"my_flow": "./graph.py:make_graph"}  # name sanitized to a valid key
    assert "make_graph" in files["graph.py"] and "compile_workflow" in files["graph.py"]
    compile(files["graph.py"], "<graph.py>", "exec")  # generated file is valid Python
    assert json.loads(files["executable.json"]) == _EXECUTABLE


def test_graph_key_sanitized():
    assert _graph_key("Hello World", "id1") == "hello_world"
    assert _graph_key("123abc", "id2") == "wf_123abc"  # must be letter-led
    assert _graph_key(None, "wf_x") == "wf_x"


def test_build_zip_roundtrips():
    data = build_zip("wf_demo", "My Flow!", _EXECUTABLE)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert "graph.py" in names and "langgraph.json" in names
        assert json.loads(zf.read("executable.json")) == _EXECUTABLE


async def test_exported_executable_compiles_and_runs_with_minimal_ctx():
    # Mirrors the generated graph.py: a minimal local CompileContext is enough to compile + run
    # the exported executable (the whole point of the export).
    ctx = CompileContext(tenant_id="local", project_id="local", checkpointer=InMemorySaver())
    graph = compile_workflow(_EXECUTABLE, ctx)
    out = await graph.ainvoke({"payload": {"a": 1}}, {"configurable": {"thread_id": "lg1"}})
    assert out["data"] == {"a": 1}


async def _owner_token() -> str:
    from ros.db import SessionLocal

    async with SessionLocal() as s:
        owner = await AuthService.register(s, email=f"u{uuid.uuid4().hex[:10]}@example.com", password="ownerpass1")
    return create_access_token(user_id=owner.id, tenant_id=owner.tenant_id, role=owner.role)


async def test_export_endpoint_404_for_missing_workflow():
    tok = await _owner_token()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/v1/projects/p1/workflows/nope/export/langgraph",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 404, r.text
