"""Project-level default tools/toolsets (project.config.default_tools / default_toolsets) are granted
to EVERY agent node, in addition to the tools the node lists itself — the tool analogue of the
default-middleware "one capability on every agent" guarantee. `resolve_tool_ids` de-dups by id, so a
node that also lists a default tool doesn't bind it twice."""

from __future__ import annotations

from types import SimpleNamespace

from ros.engine.context import CompileContext
from ros.nodes.agent_node import _common_kwargs


def _ctx(**kw) -> CompileContext:
    # `fake` model keeps resolve_model offline (no provider key / network), like test_default_middleware.
    ctx = CompileContext(tenant_id="t", project_id="p", default_model="fake", **kw)
    # A small registry: two standalone tools + a toolset whose only member is a third tool.
    ctx.tool_registry = {
        "t_shared": SimpleNamespace(name="shared_tool"),
        "t_own": SimpleNamespace(name="own_tool"),
        "t_set": SimpleNamespace(name="set_tool"),
    }
    ctx.toolset_members = {"s_default": ["t_set"]}
    return ctx


def _names(common) -> list[str]:
    return [t.name for t in common["tools"]]


def test_default_tool_reaches_agent_that_lists_none():
    # An agent node with NO tools of its own still gets the project default.
    ctx = _ctx(project_default_tools=["t_shared"])
    assert "shared_tool" in _names(_common_kwargs({"model": "fake"}, ctx))


def test_default_toolset_expands_on_every_node():
    # A default TOOLSET resolves to its member tools on every node.
    ctx = _ctx(project_default_toolsets=["s_default"])
    assert "set_tool" in _names(_common_kwargs({"model": "fake"}, ctx))


def test_default_tool_merges_with_the_nodes_own_tools():
    ctx = _ctx(project_default_tools=["t_shared"])
    names = _names(_common_kwargs({"model": "fake", "tools": ["t_own"]}, ctx))
    assert set(names) == {"own_tool", "shared_tool"}


def test_default_tool_not_double_bound_when_node_also_lists_it():
    # resolve_tool_ids de-dups by id → one binding even though it's both a default and node-listed.
    ctx = _ctx(project_default_tools=["t_shared"])
    names = _names(_common_kwargs({"model": "fake", "tools": ["t_shared"]}, ctx))
    assert names.count("shared_tool") == 1


def test_no_defaults_is_a_no_op():
    # Leaving the setting empty has zero effect — the node gets exactly its own tools.
    ctx = _ctx()
    assert _names(_common_kwargs({"model": "fake", "tools": ["t_own"]}, ctx)) == ["own_tool"]
