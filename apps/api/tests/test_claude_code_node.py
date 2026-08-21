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
        def __init__(self, result, total_cost_usd, usage, is_error=False, subtype=None,
                     errors=None, api_error_status=None):
            self.result = result
            self.total_cost_usd = total_cost_usd
            self.usage = usage
            self.is_error = is_error
            self.subtype = subtype
            self.errors = errors
            self.api_error_status = api_error_status

    async def query(prompt, options):
        if capture is not None:
            capture["prompt"] = prompt
            # The governed key is scoped to the subprocess via options.env, not the process env.
            capture["env_during_run"] = getattr(options, "kw", {}).get("env")
            capture["process_env_during_run"] = os.environ.get("ANTHROPIC_API_KEY")
        # Emit a stderr line through the SDK's callback so the node can capture the real detail.
        stderr_cb = getattr(options, "kw", {}).get("stderr")
        if stderr_cb is not None:
            stderr_cb("boom: real subprocess stderr\n")
        yield AssistantMessage([TextBlock("thinking...")])
        yield ResultMessage(result, cost, usage or {}, is_error,
                            subtype="error_during_execution" if is_error else "success",
                            errors=["tool failed"] if is_error else None,
                            api_error_status=529 if is_error else None)

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


async def test_governed_key_scoped_to_subprocess_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", provider_credentials={"anthropic": "sk-governed"})
    node = _factory()({"workspace": str(tmp_path)}, ctx)

    assert "ANTHROPIC_API_KEY" not in os.environ
    await node({"messages": [HumanMessage("hi")]})
    # The key rides options.env (SDK merges it over os.environ for the CLI subprocess only)...
    assert capture["env_during_run"] == {"ANTHROPIC_API_KEY": "sk-governed"}
    # ...and never touches the shared process env — not during the run, nor after (no cross-run race).
    assert capture["process_env_during_run"] is None
    assert "ANTHROPIC_API_KEY" not in os.environ


async def test_workspace_defaults_to_the_run_directory(monkeypatch, tmp_path):
    """No `workspace` config and no env pin -> `<ROS_WORKSPACE_ROOT>/<run_id>`, so every visit to
    the node within one run (and a resume) shares one directory instead of a fresh temp dir."""
    monkeypatch.delenv("ROS_CLAUDE_CODE_WORKSPACE", raising=False)
    monkeypatch.setattr("ros.config.settings.workspace_root", str(tmp_path / "workspaces"))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", run_id="run-abc123")

    node = _factory()({}, ctx)
    await node({"messages": [HumanMessage("hi")]})
    expected = str(tmp_path / "workspaces" / "run-abc123")

    assert capture["options"]["cwd"] == expected
    assert os.path.isdir(expected)  # created up front, so the agent's first tool call lands in it
    # A second compile of the SAME run resolves to the SAME directory (resume / loop iteration).
    assert _factory()({}, ctx) is not None
    assert capture["options"]["cwd"] == expected


async def test_workspace_precedence_and_traversal(monkeypatch, tmp_path):
    """config.workspace > ROS_CLAUDE_CODE_WORKSPACE > per-run dir; a run id that is not a single
    safe path segment must not escape the root."""
    from ros.util.workspace import resolve_workspace

    monkeypatch.setattr("ros.config.settings.workspace_root", str(tmp_path / "workspaces"))
    ctx = CompileContext(tenant_id="t", project_id="p", run_id="run-1")

    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path / "pinned"))
    assert resolve_workspace(str(tmp_path / "explicit"), ctx, prefix="p-") == str(tmp_path / "explicit")
    assert resolve_workspace(None, ctx, prefix="p-") == str(tmp_path / "pinned")

    monkeypatch.delenv("ROS_CLAUDE_CODE_WORKSPACE")
    assert resolve_workspace(None, ctx, prefix="p-") == str(tmp_path / "workspaces" / "run-1")

    # Path traversal in the run id falls back to a temp dir rather than climbing out of the root.
    evil = CompileContext(tenant_id="t", project_id="p", run_id="../../etc")
    got = resolve_workspace(None, evil, prefix="ros-claude-code-")
    assert "ros-claude-code-" in got
    assert str(tmp_path / "workspaces") not in got

    # No run id at all (preview/validation compile) -> temp dir, the pre-existing behavior.
    bare = CompileContext(tenant_id="t", project_id="p")
    assert "ros-claude-code-" in resolve_workspace(None, bare, prefix="ros-claude-code-")


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


async def test_is_error_surfaces_detail_not_placeholder(monkeypatch, tmp_path):
    from ros.nodes.claude_code import ClaudeCodeError

    _install_fake_sdk(monkeypatch, result="Command failed with exit code 1", is_error=True)
    ctx = CompileContext(tenant_id="t", project_id="p", provider_credentials={"anthropic": "sk"})
    node = _factory()({"workspace": str(tmp_path)}, ctx)

    with pytest.raises(ClaudeCodeError) as ei:
        await node({"messages": [HumanMessage("do it")]})

    err = ei.value
    # Structured detail from the terminal ResultMessage is preserved, not swallowed.
    assert err.subtype == "error_during_execution"
    assert err.api_error_status == 529
    assert err.errors == ["tool failed"]
    # The real subprocess stderr (captured via the SDK callback) is in the message + on the error.
    assert "boom: real subprocess stderr" in str(err)
    assert "boom: real subprocess stderr" in (err.stderr or "")


async def test_process_error_stderr_propagated(monkeypatch, tmp_path):
    from ros.nodes.claude_code import ClaudeCodeError

    _install_fake_sdk(monkeypatch)
    import claude_agent_sdk

    class ProcessError(Exception):
        def __init__(self, message, exit_code=None, stderr=None):
            self.exit_code = exit_code
            self.stderr = stderr
            super().__init__(message)

    async def failing_query(prompt, options):
        stderr_cb = getattr(options, "kw", {}).get("stderr")
        if stderr_cb is not None:
            stderr_cb("captured: node build error\n")
        raise ProcessError("Command failed", exit_code=1, stderr="Check stderr output for details")
        yield  # pragma: no cover - makes this an async generator

    claude_agent_sdk.query = failing_query
    ctx = CompileContext(tenant_id="t", project_id="p", provider_credentials={"anthropic": "sk"})
    node = _factory()({"workspace": str(tmp_path)}, ctx)

    with pytest.raises(ClaudeCodeError) as ei:
        await node({"messages": [HumanMessage("do it")]})

    err = ei.value
    assert err.exit_code == 1
    # Falls back to the captured stderr buffer when the SDK's own .stderr is the placeholder.
    assert "captured: node build error" in str(err)
    assert "captured: node build error" in (err.stderr or "")


async def test_missing_sdk_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # force ImportError on import
    ctx = CompileContext(tenant_id="t", project_id="p")
    node = _factory()({"workspace": str(tmp_path)}, ctx)
    with pytest.raises(ImportError, match="claude_code"):
        await node({"messages": [HumanMessage("hi")]})


# --- stable per-node workspace + repo checkout -------------------------------------------------

async def test_workspace_composed_from_base_workflow_and_node(monkeypatch, tmp_path):
    # No explicit config.workspace: the cwd must be <base>/<workflow_id>/<node_id>.
    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", workflow_id="wf1",
                         provider_credentials={"anthropic": "sk"})
    node = _factory()({}, ctx, node_id="cc")

    await node({"messages": [HumanMessage("hi")]})
    assert capture["options"]["cwd"] == str(tmp_path / "wf1" / "cc")
    assert os.path.isdir(str(tmp_path / "wf1" / "cc"))  # created


async def test_workspace_resolved_per_invocation(monkeypatch, tmp_path):
    # cwd is resolved inside _node, so a base change is picked up WITHOUT recompiling the factory.
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", workflow_id="wf", provider_credentials={"anthropic": "sk"})
    node = _factory()({}, ctx, node_id="n")

    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path / "a"))
    await node({"messages": [HumanMessage("hi")]})
    assert capture["options"]["cwd"] == str(tmp_path / "a" / "wf" / "n")

    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path / "b"))
    await node({"messages": [HumanMessage("hi")]})
    assert capture["options"]["cwd"] == str(tmp_path / "b" / "wf" / "n")


async def test_explicit_workspace_still_honored(monkeypatch, tmp_path):
    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path / "base"))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture=capture)
    ctx = CompileContext(tenant_id="t", project_id="p", workflow_id="wf", provider_credentials={"anthropic": "sk"})
    node = _factory()({"workspace": str(tmp_path / "pinned")}, ctx, node_id="cc")
    await node({"messages": [HumanMessage("hi")]})
    assert capture["options"]["cwd"] == str(tmp_path / "pinned")  # verbatim, no wf/node suffix


async def test_repo_cloned_once_with_token(monkeypatch, tmp_path):
    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture=capture)

    # Resolve the secret ref to a token.
    import ros.nodes.claude_code as mod
    monkeypatch.setattr(mod, "_resolve_repo_token", _fake_token("ghp_secret"))

    calls: list = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _FakeProc(0, b"", b"")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    ctx = CompileContext(tenant_id="t", project_id="p", workflow_id="wf", provider_credentials={"anthropic": "sk"})
    node = _factory()({"repo_url": "https://github.com/acme/repo.git", "repo_ref": "main",
                       "repo_secret_ref": "secret://p/gh"}, ctx, node_id="cc")
    await node({"messages": [HumanMessage("hi")]})

    assert len(calls) == 1
    argv = calls[0]
    assert argv[:4] == ["git", "clone", "--depth", "1"]
    assert "--branch" in argv and "main" in argv
    # Token spliced as x-access-token, into the target cwd.
    assert "https://x-access-token:ghp_secret@github.com/acme/repo.git" in argv
    assert argv[-1] == str(tmp_path / "wf" / "cc")


async def test_repo_checkout_skipped_when_git_present(monkeypatch, tmp_path):
    ws = tmp_path / "wf" / "cc"
    (ws / ".git").mkdir(parents=True)  # simulate an already-cloned workspace
    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path))
    _install_fake_sdk(monkeypatch)

    called = {"n": 0}

    async def fake_exec(*args, **kwargs):
        called["n"] += 1
        return _FakeProc(0, b"", b"")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    ctx = CompileContext(tenant_id="t", project_id="p", workflow_id="wf", provider_credentials={"anthropic": "sk"})
    node = _factory()({"repo_url": "https://github.com/acme/repo.git"}, ctx, node_id="cc")
    await node({"messages": [HumanMessage("hi")]})
    assert called["n"] == 0  # clone-once: existing .git => no git subprocess


async def test_repo_checkout_failure_scrubs_token(monkeypatch, tmp_path):
    from ros.nodes.claude_code import ClaudeCodeError

    monkeypatch.setenv("ROS_CLAUDE_CODE_WORKSPACE", str(tmp_path))
    _install_fake_sdk(monkeypatch)
    import ros.nodes.claude_code as mod
    monkeypatch.setattr(mod, "_resolve_repo_token", _fake_token("ghp_secret"))

    async def fake_exec(*args, **kwargs):
        # stderr echoes the authed URL, as git does on failure.
        return _FakeProc(128, b"", b"fatal: could not read from https://x-access-token:ghp_secret@github.com/acme/repo.git")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    ctx = CompileContext(tenant_id="t", project_id="p", workflow_id="wf", provider_credentials={"anthropic": "sk"})
    node = _factory()({"repo_url": "https://github.com/acme/repo.git", "repo_secret_ref": "secret://p/gh"},
                      ctx, node_id="cc")
    with pytest.raises(ClaudeCodeError) as ei:
        await node({"messages": [HumanMessage("hi")]})
    text = str(ei.value)
    assert "ghp_secret" not in text          # token redacted
    assert "x-access-token:***@" in text     # scrubbed marker present


def test_factory_receives_node_id_via_compiler():
    # The compiler passes node_id only to factories that accept it (back-compatible).
    from ros.engine.compiler import _build_node
    from ros.engine.registry import get_spec

    spec = get_spec("claude_code")
    ctx = CompileContext(tenant_id="t", project_id="p", workflow_id="wf")
    # Should not raise, and the node is callable.
    fn = _build_node(spec, {"workspace": ""}, ctx, "my_node")
    assert callable(fn)


def _fake_token(tok):
    async def _f(ctx, secret_ref):
        return tok if secret_ref else None
    return _f


class _FakeProc:
    def __init__(self, rc, out, err):
        self.returncode = rc
        self._out = out
        self._err = err

    async def communicate(self):
        return self._out, self._err
