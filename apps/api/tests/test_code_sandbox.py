"""Code-executor seam (WS5 5a): dispatch, tier selection, prod-guard, and the freestyle adapter's
request/response mapping (mocked). See docs/design/code-execution-sandbox.md."""

from __future__ import annotations

import pytest

from ros.config import settings
from ros.tools.code import CodeToolError, execute_code
from ros.tools.sandbox import SandboxError, get_code_executor, reset_code_executor


@pytest.fixture(autouse=True)
def _reset_executor(monkeypatch):
    monkeypatch.setattr(settings, "enable_code_tools", True)
    reset_code_executor()
    yield
    reset_code_executor()


# --- seam / tier selection ---
def test_default_executor_is_restricted_and_not_isolating():
    ex = get_code_executor()
    assert ex.name == "restricted"
    assert ex.isolating is False


def test_unknown_executor_raises(monkeypatch):
    monkeypatch.setattr(settings, "code_executor", "does-not-exist")
    reset_code_executor()
    with pytest.raises(RuntimeError, match="Unknown ROS_CODE_EXECUTOR"):
        get_code_executor()


# --- restricted tier preserves behaviour ---
async def test_execute_code_dispatches_to_restricted():
    cfg = {"source": "def main(a, b):\n    return a + b\n"}
    assert await execute_code(cfg, {"a": 2, "b": 5}) == 7


async def test_restricted_result_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "code_tool_max_result_chars", 50)
    reset_code_executor()
    cfg = {"source": "def main():\n    return 'x' * 1000\n"}
    out = await execute_code(cfg, {})
    assert isinstance(out, dict) and out["error"] == "result_too_large" and out["limit"] == 50


async def test_disabled_code_tools_raises(monkeypatch):
    monkeypatch.setattr(settings, "enable_code_tools", False)
    with pytest.raises(CodeToolError, match="disabled"):
        await execute_code({"source": "def main():\n    return 1\n"}, {})


# --- freestyle adapter (mocked transport) ---
class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    @property
    def text(self):
        import json

        return json.dumps(self._payload)


class _FakeClient:
    """Captures the outgoing request and returns a canned response."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        return _FakeResp({"stdout": '__ROS_RESULT__{"sum": 7}\n', "statusCode": 0})


def _use_freestyle(monkeypatch, client_cls=_FakeClient):
    monkeypatch.setattr(settings, "code_executor", "freestyle")
    monkeypatch.setattr(settings, "freestyle_api_key", "fs_test_key")
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", client_cls)
    reset_code_executor()


async def test_freestyle_builds_request_and_parses_result(monkeypatch):
    _use_freestyle(monkeypatch)
    out = await execute_code({"source": "def main(a, b):\n    return a + b\n"}, {"a": 3, "b": 4})
    assert out == {"sum": 7}  # parsed from the stdout sentinel
    cap = _FakeClient.captured
    assert cap["headers"]["Authorization"] == "Bearer fs_test_key"
    assert "__ROS_RESULT__" in cap["json"]["code"]        # wrapper injects the sentinel
    assert cap["json"]["language"] == "python"
    assert cap["url"].endswith(settings.freestyle_run_path)


async def test_freestyle_missing_key_raises_sandbox_error(monkeypatch):
    monkeypatch.setattr(settings, "code_executor", "freestyle")
    monkeypatch.setattr(settings, "freestyle_api_key", None)
    reset_code_executor()
    with pytest.raises(SandboxError, match="FREESTYLE_API_KEY"):
        await execute_code({"source": "def main():\n    return 1\n"}, {})


async def test_freestyle_nonzero_status_is_runtime_error(monkeypatch):
    class _ErrClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            return _FakeResp({"stdout": "", "stderr": "boom", "statusCode": 1})

    _use_freestyle(monkeypatch, _ErrClient)
    with pytest.raises(CodeToolError, match="runtime:"):
        await execute_code({"source": "def main():\n    return 1\n"}, {})


# --- prod guard: isolating executor no longer needs the unsandboxed ack ---
def _prod(monkeypatch, executor, ack):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "enable_code_tools", True)
    monkeypatch.setattr(settings, "code_executor", executor)
    monkeypatch.setattr(settings, "allow_unsandboxed_code_tools", ack)
    return [p for p in settings.validate_production() if "ENABLE_CODE_TOOLS" in p]


def test_prod_guard_blocks_restricted_without_ack(monkeypatch):
    assert _prod(monkeypatch, "restricted", ack=False), "restricted w/o ack must be flagged"


def test_prod_guard_allows_isolating_executor(monkeypatch):
    assert _prod(monkeypatch, "freestyle", ack=False) == [], "freestyle is isolating -> no ack needed"


def test_prod_guard_allows_restricted_with_ack(monkeypatch):
    assert _prod(monkeypatch, "restricted", ack=True) == []
