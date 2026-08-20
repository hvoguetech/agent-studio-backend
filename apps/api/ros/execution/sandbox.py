"""SandboxBackend — the ISOLATING execution backend (WS10 Phase 1).

Dispatches a run to a per-run sandbox that holds NO ambient authority: the sandbox process gets ONLY
`ROS_MASTER_URL` + a short-lived run-scoped token, and reaches all privileged state (manifest, secrets,
frames, status, result) through master's tenant-scoped runtime API. It does NOT receive the shared
DB/Redis URL or the master key — that omission is the isolation boundary, and the difference from the
trusted-VM `FreestyleBackend` (which injects those). See docs/design/sandbox-backend-build-plan.md.

Selected by ROS_EXECUTION_BACKEND=sandbox. Subclasses LocalBackend and overrides only `submit`;
retry / reclaim_orphans / scheduler / singleton are inherited (they act on the shared DB the control
plane owns). Falls back to LocalBackend's inline/arq path when no dispatch control service is
configured, so dev/tests keep working without a sandbox.

P1a: reuses the Freestyle control service (freestyle_control) as the DISPATCHER (persistent VM for
now, per the interim decision); the provider is swappable (E2B is the spec target) behind that seam.
Non-HITL runs only until the callback-backed checkpointer (G-C) lands.
"""

from __future__ import annotations

import logging

from ros.config import settings
from ros.execution.local import LocalBackend

logger = logging.getLogger("ros.execution.sandbox")


class SandboxBackend(LocalBackend):
    name = "sandbox"

    async def submit(self, *, run_id, tenant_id, project_id=None, run_service=None,
                     public=False, run_context=None) -> dict:
        from ros.execution import freestyle_control

        if not freestyle_control.is_enabled():
            logger.info("sandbox backend: no dispatch control service -> local submit for %s", run_id)
            return await super().submit(
                run_id=run_id, tenant_id=tenant_id, project_id=project_id, run_service=run_service,
                public=public, run_context=run_context,
            )
        run_token = self._mint_run_token(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
        sticky_key = await self._sticky_key(run_id, tenant_id) if settings.freestyle_warm_vms else None
        run_input = await self._run_input(run_id, tenant_id)
        receipt = await freestyle_control.dispatch_sandbox_run(
            run_id=run_id, tenant_id=tenant_id, project_id=project_id,
            master_url=settings.public_base_url, run_token=run_token, run_input=run_input,
            sticky_key=sticky_key, public=public, run_context=run_context,
        )
        await self._record_executor(run_id, tenant_id, receipt.get("vm_id"))
        return {"run_id": run_id, "status": "dispatched", "backend": "sandbox", **receipt}

    async def _run_input(self, run_id: str, tenant_id: str) -> dict:
        """The run's input, read from the shared DB on the CONTROL plane (not the sandbox). Passed to
        the sandbox command as --input so it need not expose a separate input endpoint in P1a."""
        from sqlalchemy import select

        from ros.db.base import SessionLocal
        from ros.db.scoping import set_current_tenant
        from ros.models import Run

        set_current_tenant(tenant_id)
        async with SessionLocal() as session:
            row = (await session.execute(
                select(Run.input).where(Run.id == run_id, Run.tenant_id == tenant_id)
            )).scalar_one_or_none()
        return row or {}

    async def _record_executor(self, run_id: str, tenant_id: str, vm_id: str | None) -> None:
        from sqlalchemy import update

        from ros.db.base import SessionLocal
        from ros.db.scoping import set_current_tenant
        from ros.models import Run

        set_current_tenant(tenant_id)
        async with SessionLocal() as session:
            await session.execute(
                update(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
                .values(executor={"driver": "sandbox", "vm_id": vm_id})
            )
            await session.commit()

    async def _sticky_key(self, run_id: str, tenant_id: str) -> str | None:
        from sqlalchemy import select

        from ros.db.base import SessionLocal
        from ros.db.scoping import set_current_tenant
        from ros.models import Run

        set_current_tenant(tenant_id)
        async with SessionLocal() as session:
            wf_id = (await session.execute(
                select(Run.workflow_id).where(Run.id == run_id, Run.tenant_id == tenant_id)
            )).scalar_one_or_none()
        return wf_id

    def _mint_run_token(self, *, run_id: str, tenant_id: str, project_id: str | None) -> str:
        from ros.security import create_run_token

        return create_run_token(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
