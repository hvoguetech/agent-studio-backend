"""Per-environment endpoint substitution ({{env.*}} from FORGE_TOOL_VARS / settings.tool_vars).

A tool/auth endpoint template references {{env.<key>}}; the value is supplied per environment via
the tool_vars setting, so the SAME tool DB row resolves to a different real host in dev/qa/prod.
Unlike {{ctx.*}} (lenient - a missing value is dropped/empty), a missing {{env.*}} key FAILS the
call loudly (MissingTemplateVar), so a misconfigured environment never sends a broken URL.
"""

import json

import httpx
import pytest

import forge.tools.graphql as gql_mod
import forge.tools.rest as rest_mod
from forge.auth_providers.templates import MissingTemplateVar
from forge.tools.graphql import execute_graphql
from forge.tools.rest import execute_rest


def _capturing_client(sink: dict) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        sink["request"] = request
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_env_substitution_in_url(monkeypatch):
    """{{env.*}} in a url_template resolves from settings.tool_vars to the env's real host."""
    monkeypatch.setattr(rest_mod.settings, "tool_vars", {"api_base": "https://api.qa.example.com"})
    sink: dict = {}
    cfg = {
        "name": "orders_get",
        "request": {
            "method": "GET",
            "url_template": "{{env.api_base}}/v1/orders/{id}",
            "fields": [{"path": "id", "type": "string", "in": "path", "required": True, "llm_visible": True}],
            "headers": [],
        },
    }
    async with _capturing_client(sink) as client:
        await execute_rest(cfg, {"id": "O-1"}, tenant_id="t", project_id="p", client=client)
    assert str(sink["request"].url) == "https://api.qa.example.com/v1/orders/O-1"


async def test_env_substitution_in_body_template(monkeypatch):
    """{{env.*}} works alongside {{input.*}} in a JSON body template."""
    monkeypatch.setattr(rest_mod.settings, "tool_vars", {"tenant": "acme-qa"})
    sink: dict = {}
    cfg = {
        "name": "order_add",
        "request": {
            "method": "POST",
            "url_template": "https://portal.example.dev/orders",
            "fields": [{"path": "amount", "type": "integer", "in": "body", "llm_visible": True}],
            "headers": [],
            "body_template": '{"amount": {{ input.amount }}, "tenant": "{{ env.tenant }}"}',
        },
    }
    async with _capturing_client(sink) as client:
        await execute_rest(cfg, {"amount": 5}, tenant_id="t", project_id="p", client=client)
    assert json.loads(sink["request"].content) == {"amount": 5, "tenant": "acme-qa"}


async def test_undefined_env_var_fails_loud(monkeypatch):
    """A template referencing an env var absent from tool_vars raises (never a broken request)."""
    monkeypatch.setattr(rest_mod.settings, "tool_vars", {})  # nothing defined for this env
    sink: dict = {}
    cfg = {
        "name": "x",
        "request": {"method": "GET", "url_template": "{{env.api_base}}/x", "fields": [], "headers": []},
    }
    async with _capturing_client(sink) as client:
        with pytest.raises(MissingTemplateVar) as e:
            await execute_rest(cfg, {}, tenant_id="t", project_id="p", client=client)
    assert "api_base" in str(e.value)
    assert "request" not in sink  # the call never went out


async def test_ctx_stays_lenient_alongside_strict_env(monkeypatch):
    """env is strict, but ctx keeps its lenient behavior (missing -> empty) in the same template."""
    monkeypatch.setattr(rest_mod.settings, "tool_vars", {"base": "https://api.example.com"})
    sink: dict = {}
    cfg = {
        "name": "x",
        "request": {
            "method": "GET",
            "url_template": "{{env.base}}/x?tok={{ctx.absent}}",
            "fields": [],
            "headers": [],
        },
    }
    async with _capturing_client(sink) as client:
        await execute_rest(cfg, {}, tenant_id="t", project_id="p", context={}, client=client)
    # env resolved; the missing ctx token rendered empty rather than raising.
    assert str(sink["request"].url) == "https://api.example.com/x?tok="


async def test_graphql_endpoint_env_substitution(monkeypatch):
    """The GraphQL endpoint (previously used verbatim) now resolves {{env.*}} too."""
    monkeypatch.setattr(gql_mod.settings, "tool_vars", {"gql_base": "https://gql.prod.example.com"})
    sink: dict = {}
    cfg = {"endpoint": "{{env.gql_base}}/graphql", "query": "{ ping }", "variables": []}
    async with _capturing_client(sink) as client:
        await execute_graphql(cfg, {}, tenant_id="t", project_id="p", client=client)
    assert str(sink["request"].url) == "https://gql.prod.example.com/graphql"
