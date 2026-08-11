"""Model routing: project aliases + the OpenRouter gateway provider."""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from ros.config import settings
from ros.engine.context import CompileContext
from ros.engine.models import resolve_model


def test_model_alias_expands_to_target():
    ctx = CompileContext(tenant_id="t", project_id="p", model_aliases={"fast": "fake:cheap-reply"})
    m = resolve_model("fast", ctx)
    assert m.invoke([HumanMessage(content="hi")]).content == "cheap-reply"


def test_openrouter_provider_builds_openai_client(monkeypatch):
    captured: dict = {}

    def fake_init(model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return object()

    import langchain.chat_models as lcm

    monkeypatch.setattr(lcm, "init_chat_model", fake_init)
    monkeypatch.setattr(settings, "llm_http_keepalive", False)  # avoid attaching a real client
    ctx = CompileContext(tenant_id="t", project_id="p", provider_credentials={"openrouter": "OR_KEY"})
    resolve_model("openrouter:anthropic/claude-sonnet-4.6", ctx)
    assert captured["model"] == "anthropic/claude-sonnet-4.6"  # prefix stripped, vendor/model kept
    assert captured["kwargs"]["model_provider"] == "openai"
    assert captured["kwargs"]["api_key"] == "OR_KEY"
    assert "openrouter.ai" in captured["kwargs"]["base_url"]


def test_openrouter_falls_back_to_env_key(monkeypatch):
    captured: dict = {}
    import langchain.chat_models as lcm

    monkeypatch.setattr(lcm, "init_chat_model", lambda model, **kw: captured.update(kw) or object())
    monkeypatch.setattr(settings, "llm_http_keepalive", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "ENV_OR_KEY")
    resolve_model("openrouter:openai/gpt-5.2", CompileContext(tenant_id="t", project_id="p"))
    assert captured["api_key"] == "ENV_OR_KEY"
