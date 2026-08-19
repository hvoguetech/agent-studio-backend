"""claude_code node — SDK adaptation, credential injection, and schema validation.

The Claude Agent SDK is a lazy/optional import (drives the `claude` CLI subprocess), so these
tests inject a fake `claude_agent_sdk` module: no CLI or network needed.
"""

from __future__ import annotations

import os
import sys
import types

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ros.engine.context import CompileContext
from ros.services.validation import validate_workflow


def _install_fake_sdk(monkeypatch, *, result="Done", cost=0.01, usage=None, is_error=False, capture=None):
    fake = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw
            if capture is not None:
                capture["options"] = kw

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, result, total_cost_usd, usage, is_error=False):
            self.result = result
            self.total_cost_usd = total_cost_usd
            self.usage = usage
            self.is_error = is_error

    async def query(prompt, options):
        if capture is not None:
            capture["prompt"] = prompt
            capture["key_during_run"] = os.environ.get("ANTHROPIC_API_KEY")
        yield AssistantMessage([TextBlock("thinking...")])
        yield ResultMessage(result, cost, usage or {}, is_error)

    for name, obj in [
        ("ClaudeAgentOptions", ClaudeAgentOptions), ("TextBlock", TextBlock),
        ("AssistantMessage", AssistantMessage), ("ResultMessage", ResultMessage), ("query", query),
    ]:
        setattr(fake, name, obj)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def _factory():
    from ros.nodes.claude_code import claude_code_factory

    return claude_code_factory


async def test_result_message_maps_to_ai_message(monkeypatch, tmp_path):
    capture: dict = {}
    _install_fake_sdk(monkeypatch, result="Created hello.py", cost=0.0123,
                      usage={"input_tokens": 10, "output_tokens": 5}, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", provider_credentials={"anthropic": "sk-governed"})
    node = _factory()({"model": "claude-sonnet-4-5", "workspace": str(tmp_path)}, ctx)

    out = await node({"messages": [HumanMessage("write hello.py")]})
    msg = out["messages"][0]

    assert isinstance(msg, AIMessage)
    assert msg.content == "Created hello.py"  # ResultMessage.result is authoritative
    meta = msg.response_metadata["claude_code"]
    assert meta["cost_usd"] == 0.0123
    assert meta["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert capture["prompt"] == "write hello.py"
    assert capture["options"]["cwd"] == str(tmp_path)


async def test_governed_key_injected_only_during_run(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", provider_credentials={"anthropic": "sk-governed"})
    node = _factory()({"workspace": str(tmp_path)}, ctx)

    assert "ANTHROPIC_API_KEY" not in os.environ
    await node({"messages": [HumanMessage("hi")]})
    assert capture["key_during_run"] == "sk-governed"      # visible to the subprocess mid-run
    assert "ANTHROPIC_API_KEY" not in os.environ           # cleaned up after (no leak into the process)


async def test_empty_input_short_circuits(monkeypatch, tmp_path):
    _install_fake_sdk(monkeypatch)
    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"workspace": str(tmp_path)}, ctx)
    out = await node({"messages": []})
    assert "no input prompt" in out["messages"][0].content


def test_registered_and_schema_validates():
    from ros.engine.registry import get_spec

    spec = get_spec("claude_code")
    assert spec.label == "Claude Code"
    assert [p.io_type for p in spec.input_ports] == ["messages"]

    wf = {
        "id": "w", "version": 1,
        "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
        "entry_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "cc", "type": "claude_code",
             "config": {"model": "claude-sonnet-4-5", "permission_mode": "acceptEdits",
                        "max_turns": 20, "allowed_tools": ["Read", "Edit", "Bash"]}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [{"source": "start", "target": "cc"}, {"source": "cc", "target": "end"}],
    }
    res = validate_workflow(wf)
    assert res.valid, res.errors


def test_invalid_config_rejected():
    wf = {
        "id": "w", "version": 1,
        "state": {"messages": {"type": "list[message]", "reducer": "add_messages"}},
        "entry_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "cc", "type": "claude_code", "config": {"permission_mode": "nope", "bogus": 1}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [{"source": "start", "target": "cc"}, {"source": "cc", "target": "end"}],
    }
    res = validate_workflow(wf)
    assert not res.valid


async def test_missing_sdk_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # force ImportError on import
    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"workspace": str(tmp_path)}, ctx)
    with pytest.raises(ImportError, match="claude_code"):
        await node({"messages": [HumanMessage("hi")]})
