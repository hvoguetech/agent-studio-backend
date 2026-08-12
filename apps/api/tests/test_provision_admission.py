"""Provisioning admission (max_backends) — folded into ros budget alongside the run spend caps."""

from __future__ import annotations

import pytest

from ros.db.base import SessionLocal
from ros.models import Project, ProvisionedBackend
from ros.services.budget import ProvisionNotAllowed, enforce_provision_admission


async def test_admission_noop_without_project_or_cap():
    async with SessionLocal() as s:
        await enforce_provision_admission(s, "t_np", "p_np")  # missing project -> no-op
        proj = Project(tenant_id="t_nc", name="nc", slug="nc-slug", config={})  # no budgets.max_backends
        s.add(proj)
        await s.commit()
        await s.refresh(proj)
        await enforce_provision_admission(s, "t_nc", proj.id)  # unconfigured -> no-op


async def test_admission_enforces_max_backends():
    async with SessionLocal() as s:
        proj = Project(tenant_id="t_cap", name="c", slug="c-cap", config={"budgets": {"max_backends": 1}})
        s.add(proj)
        await s.commit()
        await s.refresh(proj)
        pid = proj.id

        await enforce_provision_admission(s, "t_cap", pid)  # 0 existing -> ok

        s.add(ProvisionedBackend(tenant_id="t_cap", project_id=pid, provider="railway-postgres", status="active"))
        await s.commit()
        with pytest.raises(ProvisionNotAllowed):
            await enforce_provision_admission(s, "t_cap", pid)  # at cap
