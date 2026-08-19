"""Artifact plane (docs/specs/artifacts-and-code-node.md slice 1): the `artifacts` state channel,
its merge-by-(bucket,key) reducer, the ref<->state bridge, and the producer->consumer handoff
through a real compiled graph — including the invariant that bytes never enter the checkpoint."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ros.artifacts import ArtifactRef, ArtifactStore, ObjectStoreError
from ros.artifacts.backends import LocalObjectStore
from ros.artifacts.state import (
    ARTIFACTS_KEY,
    emit,
    from_entry,
    load,
    run_scope,
    select,
    to_entry,
)
from ros.engine.node_io import apply_edge_mappings
from ros.engine.state import REDUCERS, build_state_typeddict

_reduce = REDUCERS["artifacts"]


def _entry(key: str, *, bucket: str = "b", produced_by: str | None = None, size: int = 1) -> dict:
    return to_entry(
        ArtifactRef(bucket=bucket, key=key, sha256=key, size=size, filename=key.split("/")[-1]),
        produced_by=produced_by,
    )


def _store(tmp_path) -> ArtifactStore:
    return ArtifactStore(LocalObjectStore(str(tmp_path)))


# --- reducer semantics ---
def test_reducer_appends_and_preserves_order():
    out = _reduce([_entry("a")], [_entry("b"), _entry("c")])
    assert [e["key"] for e in out] == ["a", "b", "c"]


def test_reducer_updates_in_place_by_identity():
    """A re-emitted artifact (same content-addressed key) updates its slot, never duplicates —
    this is what makes a resume/retry a no-op instead of growing the channel."""
    out = _reduce([_entry("a", produced_by="n1"), _entry("b")], [_entry("a", produced_by="n2")])
    assert [e["key"] for e in out] == ["a", "b"]  # order preserved, no duplicate
    assert out[0]["produced_by"] == "n2"          # last write wins in place


def test_reducer_distinguishes_same_key_in_different_buckets():
    out = _reduce([_entry("k", bucket="b1")], [_entry("k", bucket="b2")])
    assert len(out) == 2


def test_reducer_merges_parallel_branches_without_clobbering():
    left, right = _reduce([], [_entry("a")]), _reduce([], [_entry("b")])
    assert [e["key"] for e in _reduce(left, right)] == ["a", "b"]


def test_reducer_tolerates_none_and_bare_entry():
    assert _reduce(None, None) == []
    assert [e["key"] for e in _reduce(None, _entry("a"))] == ["a"]  # a node wrote one entry, not a list


def test_reducer_does_not_mutate_its_inputs():
    left = [_entry("a")]
    _reduce(left, [_entry("b"), _entry("a", produced_by="x")])
    assert len(left) == 1 and left[0]["produced_by"] is None


def test_reducer_keeps_identityless_entries():
    """Malformed entries pass through rather than vanishing — a dropped artifact is worse than
    a visibly wrong one."""
    out = _reduce([{"junk": 1}], [_entry("a")])
    assert out == [{"junk": 1}, _entry("a")]


# --- default channel ---
def test_artifacts_is_a_default_channel():
    schema = build_state_typeddict({})
    assert ARTIFACTS_KEY in schema.__annotations__
    assert schema.__annotations__[ARTIFACTS_KEY].__metadata__[0] is _reduce


def test_declared_artifacts_channel_is_not_overridden():
    schema = build_state_typeddict({"artifacts": {"type": "list[str]", "reducer": "add"}})
    assert schema.__annotations__[ARTIFACTS_KEY].__metadata__[0] is not _reduce


# --- ref <-> state bridge ---
def test_entry_round_trip_and_metadata_split():
    ref = ArtifactRef(bucket="b", key="k", sha256="s", size=3, content_type="text/plain",
                      filename="r.txt")
    entry = to_entry(ref, produced_by="builder")
    assert entry["produced_by"] == "builder"      # graph metadata rides the entry...
    assert from_entry(entry) == ref               # ...and is not part of the durable pointer
    assert from_entry(ref) is ref


def test_from_entry_rejects_a_non_entry():
    with pytest.raises(ObjectStoreError, match="not an artifact entry"):
        from_entry({"produced_by": "n1"})


async def test_emit_is_deterministic_for_the_same_bytes(tmp_path):
    """Content-addressing + no timestamps in the entry => a replayed emit reproduces the entry
    byte-for-byte, so the reducer sees an update, not an append."""
    st = _store(tmp_path)
    kw = dict(tenant_id="t", project_id="p", run_id="r", data=b"same", filename="o.txt",
              produced_by="n1", store=st)
    assert await emit(**kw) == await emit(**kw)


def test_select_filters_and_ignores_junk():
    state = {ARTIFACTS_KEY: [
        _entry("a", produced_by="n1"), _entry("b", produced_by="n2"), "not-an-entry",
    ]}
    assert [e["key"] for e in select(state, produced_by="n1")] == ["a"]
    assert [e["key"] for e in select(state)] == ["a", "b"]
    assert select(state, filename="nope") == []
    assert select(None) == []


def test_run_scope_prefers_run_id_then_thread_id():
    assert run_scope({"configurable": {"run_id": "r1", "thread_id": "t1"}}) == "r1"
    assert run_scope({"configurable": {"thread_id": "t1"}}) == "t1"
    assert run_scope(None) == "adhoc"


def test_edge_mappings_can_select_an_artifact_with_jmespath():
    """The plane needs no new routing primitive: an edge `mappings` JMESPath already reaches
    into the channel."""
    update = {ARTIFACTS_KEY: [_entry("a", produced_by="writer"), _entry("b", produced_by="other")]}
    mapped = apply_edge_mappings(
        [{"from": "artifacts[?produced_by=='writer'] | [0]", "to": "picked"}], update, {},
    )
    assert mapped["picked"]["key"] == "a"


# --- producer -> consumer through a compiled graph ---
def _graph(store, *, producers=("writer",), echo=True):
    schema = build_state_typeddict({"seen": {"type": "str", "reducer": "last"}})
    g = StateGraph(schema)

    def _producer(name: str):
        async def _fn(state):
            entry = await emit(tenant_id="t", project_id="p", run_id="r",
                               data=f"PAYLOAD-FROM-{name}".encode(), filename=f"{name}.txt",
                               content_type="text/plain", produced_by=name, store=store)
            return {ARTIFACTS_KEY: [entry]}
        return _fn

    async def reader(state):
        entries = select(state, produced_by=producers[0])
        data = await load(entries[0], store=store)
        # echo=False: prove the node read the bytes WITHOUT putting them back into state, so the
        # checkpoint assertion measures the plane rather than the test's own consumer.
        return {"seen": data.decode() if echo else f"len={len(data)}"}

    for name in producers:
        g.add_node(name, _producer(name))
        g.add_edge(START, name)
        g.add_edge(name, "reader")
    g.add_node("reader", reader)
    g.add_edge("reader", END)
    return g


async def test_producer_hands_an_artifact_to_a_downstream_node(tmp_path):
    saver = InMemorySaver()
    graph = _graph(_store(tmp_path)).compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "th-1", "run_id": "r"}}

    out = await graph.ainvoke({}, config)

    assert out["seen"] == "PAYLOAD-FROM-writer"          # consumer read the bytes back
    [entry] = out[ARTIFACTS_KEY]
    assert entry["produced_by"] == "writer" and entry["filename"] == "writer.txt"
    assert entry["key"].endswith(f"/{entry['sha256']}/writer.txt")


async def test_bytes_never_enter_the_checkpoint(tmp_path):
    """The invariant the whole plane exists for (docs/design/artifact-storage.md §3)."""
    saver = InMemorySaver()
    graph = _graph(_store(tmp_path), echo=False).compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "th-2", "run_id": "r"}}
    out = await graph.ainvoke({}, config)
    assert out["seen"] == "len=19"                    # the consumer did read the bytes

    checkpoint = repr((await saver.aget_tuple(config)).checkpoint)
    assert "PAYLOAD-FROM-writer" not in checkpoint    # the bytes stayed in the store...
    assert "writer.txt" in checkpoint                 # ...only the reference was checkpointed


async def test_parallel_producers_both_survive_the_merge(tmp_path):
    graph = _graph(_store(tmp_path), producers=("writer", "other")).compile()
    out = await graph.ainvoke({}, {"configurable": {"thread_id": "th-3", "run_id": "r"}})
    assert sorted(e["produced_by"] for e in out[ARTIFACTS_KEY]) == ["other", "writer"]
