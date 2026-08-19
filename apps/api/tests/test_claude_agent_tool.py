"""claude_agent builtin tool: exposes the Claude Agent SDK as a callable tool an agent invokes.

The SDK is a lazy/optional import (drives the `claude` CLI subprocess), so these tests inject a
fake `claude_agent_sdk` module — no CLI or network needed. They cover the tool build, message →
result mapping, governed-key injection (and no leak), and the LIVE per-turn activity frames the
tool emits on the "claude_agent" stream channel.
"""

from __future__ import annotations

import os
import sys
import types

from ros.engine.context import CompileContext
from ros.schemas.contracts import validate_against_id
from ros.tools.builtin import build_builtin_tool


def _install_fake_sdk(monkeypatch, *, result="Done", cost=0.02, is_error=False, capture=None):
    fake = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw
            if capture is not None:
                capture["options"] = kw

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ToolUseBlock:
        def __init__(self, name, inp):
            self.name = name
            self.input = inp

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, result, total_cost_usd, is_error=False):
            self.result = result
            self.total_cost_usd = total_cost_usd
            self.is_error = is_error

    async def query(prompt, options):
        if capture is not None:
            capture["prompt"] = prompt
            capture["key_during_run"] = os.environ.get("ANTHROPIC_API_KEY")
        yield AssistantMessage([TextBlock("Working on it.")])
        yield AssistantMessage([ToolUseBlock("Bash", {"command": "ls"})])
        yield ResultMessage(result, cost, is_error)

    for n, o in [
        ("ClaudeAgentOptions", ClaudeAgentOptions), ("TextBlock", TextBlock),
        ("ToolUseBlock", ToolUseBlock), ("AssistantMessage", AssistantMessage),
        ("ResultMessage", ResultMessage), ("query", query),
    ]:
        setattr(fake, n, o)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def _capture_stream_frames(monkeypatch):
    frames: list = []
    import langgraph.config as lc

    monkeypatch.setattr(lc, "get_stream_writer", lambda: (lambda f: frames.append(f)), raising=False)
    return frames


def _tool(ctx, cfg=None):
    base = {"builtin": "claude_agent", "name": "claude_agent"}
    base.update(cfg or {})
    return build_builtin_tool(base, ctx)


async def test_tool_builds_with_call_args(monkeypatch):
    _install_fake_sdk(monkeypatch)
    tool = _tool(CompileContext(tenant_id="t", project_id="p"))
    assert tool.name == "claude_agent"
    assert sorted(tool.args_schema.model_fields) == ["model", "system_prompt", "task", "workspace"]


async def test_result_and_governed_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    capture: dict = {}
    _install_fake_sdk(monkeypatch, result="Refactored auth.py; tests pass.", cost=0.0345, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", provider_credentials={"anthropic": "sk-governed"})
    tool = _tool(ctx, {"workspace": str(tmp_path), "permission_mode": "acceptEdits", "max_turns": 30})

    out = await tool.coroutine(task="refactor auth.py", model="claude-sonnet-4-5")

    assert out == "Refactored auth.py; tests pass."         # ResultMessage.result is authoritative
    assert capture["prompt"] == "refactor auth.py"
    assert capture["options"]["cwd"] == str(tmp_path)
    assert capture["options"]["model"] == "claude-sonnet-4-5"
    assert capture["key_during_run"] == "sk-governed"       # visible to the subprocess mid-run
    assert "ANTHROPIC_API_KEY" not in os.environ            # cleaned up after (no leak)


async def test_live_per_turn_activity_frames(monkeypatch, tmp_path):
    frames = _capture_stream_frames(monkeypatch)
    _install_fake_sdk(monkeypatch, result="done", cost=0.01)
    ctx = CompileContext(tenant_id="t", project_id="p")
    tool = _tool(ctx, {"workspace": str(tmp_path)})

    await tool.coroutine(task="do a thing")

    events = [(f["channel"], f["payload"]["event"]) for f in frames]
    assert ("claude_agent", "start") in events
    assert ("claude_agent", "assistant") in events
    assert ("claude_agent", "tool_use") in events          # per-turn tool activity streamed
    assert ("claude_agent", "done") in events
    tool_use = next(f["payload"] for f in frames if f["payload"]["event"] == "tool_use")
    assert tool_use["tool"] == "Bash"


def test_tool_row_validates_against_schema():
    ok = validate_against_id(
        {"name": "claude_agent", "kind": "builtin", "builtin": "claude_agent",
         "description": "Delegate a task to the Claude agent.", "permission_mode": "acceptEdits", "max_turns": 30},
        "ros/tool",
    )
    assert ok == []
    bad = validate_against_id(
        {"name": "x", "kind": "builtin", "builtin": "not_a_real_builtin", "description": "x"}, "ros/tool"
    )
    assert bad  # unknown builtin still rejected
