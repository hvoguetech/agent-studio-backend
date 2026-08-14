"""FreestyleBackend — run non-interactive runs on a Freestyle VM via the standalone ros runtime.

Selected by ROS_EXECUTION_BACKEND=freestyle. Subclasses LocalBackend and overrides ONLY `submit`:
the run is dispatched to a Freestyle VM that boots `python -m ros.runtime run` (the manifest-driven
runtime), which drives the graph and writes state to the shared Postgres. Everything else — retry,
reclaim_orphans, the scheduler tick, and the singleton gate — is inherited from LocalBackend, since
those operate on that same shared DB and are substrate-agnostic (the reaper still recovers a VM run
whose driver died, the scheduler still fires due triggers).

Falls back to LocalBackend's inline/arq path when the Freestyle control service isn't configured, so
dev and tests keep working without a VM.
"""

from __future__ import annotations

import logging

from ros.config import settings
from ros.execution.local import LocalBackend

logger = logging.getLogger("ros.execution.freestyle")


class FreestyleBackend(LocalBackend):
    name = "freestyle"

    async def submit(self, *, run_id, tenant_id, project_id=None, run_service=None,
                     public=False, run_context=None) -> dict:
        from ros.execution import freestyle_control

        if not freestyle_control.is_enabled():
            # No control service -> behave exactly like local (inline/arq). Keeps dev/tests working.
            logger.info("freestyle backend: ROS_FREESTYLE_SERVICE_URL unset -> local submit for %s", run_id)
            return await super().submit(
                run_id=run_id, tenant_id=tenant_id, project_id=project_id, run_service=run_service,
                public=public, run_context=run_context,
            )
        run_token = self._mint_run_token(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
        # Warm-VM mode: key stickiness by the agent (the run's workflow id) so freestyle-svc reuses
        # one warm VM per agent across its runs, instead of cold-booting per run.
        sticky_key = await self._sticky_key(run_id, tenant_id) if settings.freestyle_warm_vms else None
        receipt = await freestyle_control.dispatch_run(
            run_id=run_id, tenant_id=tenant_id, project_id=project_id,
            master_url=settings.public_base_url, run_token=run_token, sticky_key=sticky_key,
            public=public, run_context=run_context,
        )
        # Record WHERE the run executes (the receipt carries the vm_id) so the Traces view can show
        # it ran on an isolated VM. Targeted UPDATE so it can't lose-update the VM's concurrent
        # status/heartbeat writes.
        await self._record_executor(run_id, tenant_id, receipt.get("vm_id"))
        return {"run_id": run_id, "status": "dispatched", "backend": "freestyle", **receipt}

    async def _record_executor(self, run_id: str, tenant_id: str, vm_id: str | None) -> None:
        from sqlalchemy import update

        from ros.db.base import SessionLocal
        from ros.db.scoping import set_current_tenant
        from ros.models import Run

        set_current_tenant(tenant_id)
        async with SessionLocal() as session:
            await session.execute(
                update(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
                .values(executor={"driver": "freestyle", "vm_id": vm_id})
            )
            await session.commit()

    async def _sticky_key(self, run_id: str, tenant_id: str) -> str | None:
        """The agent's stable warm-VM key = the run's workflow id (each agent gets its own VM)."""
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
        """Short-lived, run-scoped token the VM presents to master to pull the run manifest
        (scope runtime:pull, run-bound, expiring, revocable)."""
        from ros.security import create_run_token

        return create_run_token(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
