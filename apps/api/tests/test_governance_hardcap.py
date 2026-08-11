"""Governance hard-caps: admission checks EVERY node model, and a per-run USD hard cap is
enforced pre-action (before_model) by default from the project budget."""

from __future__ import annotations

import uuid

import pytest

from ros.db import SessionLocal
from ros.engine.middleware_compiler import _TenantBudgetMiddleware
from ros.models import Project
from ros.services.budget import ModelNotAllowed, disallowed_workflow_models, enforce_project_budget
from ros.services.runtime import build_compile_context


def test_disallowed_workflow_models_collects_bad_models():
    cfg = {"allowed_models": ["openai:gpt-4.1-mini"]}
    ex = {"nodes": [
        {"config": {"model": "openai:gpt-4.1-mini"}},
        {"config": {"model": "anthropic:claude-x"}},
    ]}
    assert disallowed_workflow_models(cfg, ex) == ["anthropic:claude-x"]
    assert disallowed_workflow_models({}, ex) == []  # no allow-list -> no-op


def test_tenant_budget_run_usd_cap_stops_before_model():
    mw = _TenantBudgetMiddleware(max_tokens_per_run=None, max_usd_per_thread=None, max_usd_per_run=0.01)
    stopped = mw.before_model({"_ros_run_cost_usd": 0.02})
    assert stopped and stopped.get("jump_to") == "end"
    assert mw.before_model({"_ros_run_cost_usd": 0.005}) is None  # under cap -> continue


async def _project(config: dict) -> Project:
    async with SessionLocal() as s:
        p = Project(tenant_id=f"t_{uuid.uuid4().hex[:8]}", name="P", slug=f"p{uuid.uuid4().hex[:8]}", config=config)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p


async def test_admission_rejects_disallowed_node_model():
    p = await _project({"allowed_models": ["openai:gpt-4.1-mini"]})
    ex = {"nodes": [{"config": {"model": "anthropic:claude-x"}}]}
    async with SessionLocal() as s:
        with pytest.raises(ModelNotAllowed):
            await enforce_project_budget(s, p.tenant_id, p.id, executable=ex)


async def test_admission_allows_permitted_models():
    p = await _project({"allowed_models": ["openai:gpt-4.1-mini"]})
    ex = {"nodes": [{"config": {"model": "openai:gpt-4.1-mini"}}]}
    async with SessionLocal() as s:
        await enforce_project_budget(s, p.tenant_id, p.id, executable=ex)  # no raise


async def test_runtime_autoinjects_per_run_budget_cap():
    p = await _project({"budgets": {"max_usd_per_run": 0.5}})
    async with SessionLocal() as s:
        ctx = await build_compile_context(s, tenant_id=p.tenant_id, project_id=p.id)
    tb = [e for e in ctx.project_default_mw if isinstance(e, dict) and e.get("type") == "tenant_budget"]
    assert tb and tb[0]["config"].get("max_usd_per_run") == 0.5


async def test_runtime_no_budget_no_injection():
    p = await _project({})
    async with SessionLocal() as s:
        ctx = await build_compile_context(s, tenant_id=p.tenant_id, project_id=p.id)
    assert not any(isinstance(e, dict) and e.get("type") == "tenant_budget" for e in ctx.project_default_mw)
