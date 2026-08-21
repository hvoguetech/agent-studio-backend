"""emit_artifact node — write text/HTML from state to a file artifact on the `artifacts` channel.

Logic (content selection, code-fence unwrap, content-type inference, run/produced-by) is checked
with a fake emit; one round-trip test proves it actually stores + reloads through a local store.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from ros.engine.context import CompileContext
from ros.services.validation import validate_workflow


def _factory():
    from ros.nodes.data import emit_artifact_factory

    return emit_artifact_factory


async def test_last_message_html_fence_stripped_and_type_inferred(monkeypatch):
    import ros.nodes.data as data

    captured: dict = {}

    async def fake_emit(**kw):
        captured.update(kw)
        return {"bucket": "b", "key": "k", "sha256": "k", "size": len(kw["data"]),
                "filename": kw["filename"], "content_type": kw["content_type"], "produced_by": kw.get("produced_by")}

    monkeypatch.setattr(data, "_emit_artifact", fake_emit)
    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"filename": "mockup.html"}, ctx, node_id="art")

    out = await node({"messages": [AIMessage("```html\n<h1>Hi</h1>\n```")]}, {"configurable": {"run_id": "run-1"}})

    assert list(out.keys()) == ["artifacts"]
    assert captured["data"] == b"<h1>Hi</h1>"          # single ```html fence unwrapped
    assert captured["content_type"] == "text/html"     # inferred from .html
    assert captured["filename"] == "mockup.html"
    assert captured["run_id"] == "run-1"               # from configurable.run_id
    assert captured["produced_by"] == "art"            # node id
    assert out["artifacts"][0]["filename"] == "mockup.html"


async def test_source_key_explicit_type_and_no_unwrap(monkeypatch):
    import ros.nodes.data as data

    captured: dict = {}

    async def fake_emit(**kw):
        captured.update(kw)
        return {"key": "k", "filename": kw["filename"]}

    monkeypatch.setattr(data, "_emit_artifact", fake_emit)
    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"source_key": "draft", "filename": "d.txt",
                       "content_type": "text/x-custom", "unwrap_code_fence": False}, ctx, node_id="art")

    await node({"draft": "```keep this fence```"}, {"configurable": {"run_id": "r"}})

    assert captured["data"] == b"```keep this fence```"  # unwrap disabled: verbatim
    assert captured["content_type"] == "text/x-custom"   # explicit type wins over inference


async def test_dict_source_serialized_to_json(monkeypatch):
    import ros.nodes.data as data

    captured: dict = {}

    async def fake_emit(**kw):
        captured.update(kw)
        return {"key": "k", "filename": kw["filename"]}

    monkeypatch.setattr(data, "_emit_artifact", fake_emit)
    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"source_key": "cfg", "filename": "cfg.json"}, ctx, node_id="art")

    await node({"cfg": {"a": 1}}, {"configurable": {"run_id": "r"}})

    assert captured["data"] == b'{\n  "a": 1\n}'
    assert captured["content_type"] == "application/json"


async def test_round_trip_through_local_store(monkeypatch, tmp_path):
    import ros.artifacts.state as art_state
    from ros.artifacts import ArtifactStore
    from ros.artifacts.backends import LocalObjectStore
    from ros.artifacts.state import load

    st = ArtifactStore(LocalObjectStore(str(tmp_path)))
    monkeypatch.setattr(art_state, "get_artifact_store", lambda: st)  # what emit() resolves

    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"filename": "mockup.html"}, ctx, node_id="art")

    out = await node({"messages": [AIMessage("<h1>Hi</h1>")]}, {"configurable": {"run_id": "run-1"}})
    entry = out["artifacts"][0]

    assert entry["filename"] == "mockup.html"
    assert entry["content_type"] == "text/html"
    assert await load(entry, store=st) == b"<h1>Hi</h1>"   # bytes really landed in the store


def _install_fake_stream_writer(monkeypatch):
    """Capture frames a node emits via langgraph's get_stream_writer()."""
    import langgraph.config as lgc

    frames: list = []
    monkeypatch.setattr(lgc, "get_stream_writer", lambda: frames.append, raising=False)
    return frames


def _by_channel(frames: list, channel: str) -> list:
    return [f["payload"] for f in frames if f.get("channel") == channel]


async def test_html_preview_and_download_frames_emitted(monkeypatch):
    import ros.nodes.data as data

    async def fake_emit(**kw):
        return {"bucket": "b", "key": "k", "sha256": "s", "size": len(kw["data"]),
                "filename": kw["filename"], "content_type": kw["content_type"]}

    monkeypatch.setattr(data, "_emit_artifact", fake_emit)
    frames = _install_fake_stream_writer(monkeypatch)
    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"filename": "mockup.html"}, ctx, node_id="mock")  # preview defaults on

    await node({"messages": [AIMessage("```html\n<h1>Hi</h1>\n```")]}, {"configurable": {"run_id": "r"}})

    # component frame = interactive preview
    comp = _by_channel(frames, "component")
    assert len(comp) == 1
    assert comp[0]["raw"] is True
    assert comp[0]["html"] == "<h1>Hi</h1>"          # fence-stripped, rendered verbatim
    assert comp[0]["name"] == "mockup.html"
    # artifact frame = download ref (always, for any file type)
    art = _by_channel(frames, "artifact")
    assert len(art) == 1
    assert art[0]["key"] == "k" and art[0]["bucket"] == "b"
    assert art[0]["filename"] == "mockup.html"


async def test_download_frame_always_no_preview_for_non_html(monkeypatch):
    import ros.nodes.data as data

    async def fake_emit(**kw):
        return {"bucket": "b", "key": "k", "filename": kw["filename"], "content_type": kw["content_type"]}

    monkeypatch.setattr(data, "_emit_artifact", fake_emit)
    ctx = CompileContext(tenant_id="t", project_id="p")

    # preview OFF, HTML content: download frame yes, preview frame no
    frames = _install_fake_stream_writer(monkeypatch)
    node = _factory()({"filename": "mockup.html", "preview": False}, ctx, node_id="m")
    await node({"messages": [AIMessage("<h1>Hi</h1>")]}, {"configurable": {"run_id": "r"}})
    assert _by_channel(frames, "component") == []
    assert len(_by_channel(frames, "artifact")) == 1

    # non-HTML artifact: download frame yes, preview frame no
    frames2 = _install_fake_stream_writer(monkeypatch)
    node2 = _factory()({"filename": "data.json", "source_key": "cfg"}, ctx, node_id="m")
    await node2({"cfg": {"a": 1}}, {"configurable": {"run_id": "r"}})
    assert _by_channel(frames2, "component") == []
    assert len(_by_channel(frames2, "artifact")) == 1


def test_registered_and_schema_validates():
    from ros.engine.registry import get_spec

    spec = get_spec("emit_artifact")
    assert spec.label == "Emit Artifact"

    wf = {
        "id": "w", "version": 1,
        "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
        "entry_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "gen", "type": "llm", "config": {"model": "openai:gpt-4o-mini", "prompt": "make an HTML mock"}},
            {"id": "save", "type": "emit_artifact",
             "config": {"filename": "mockup.html", "unwrap_code_fence": True}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "gen"},
            {"source": "gen", "target": "save"},
            {"source": "save", "target": "end"},
        ],
    }
    res = validate_workflow(wf)
    assert res.valid, res.errors


def test_bad_config_rejected():
    res = validate_workflow({
        "id": "w", "version": 1,
        "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
        "entry_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "save", "type": "emit_artifact", "config": {"bogus": 1}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [{"source": "start", "target": "save"}, {"source": "save", "target": "end"}],
    })
    assert not res.valid
