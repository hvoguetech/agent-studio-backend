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

    async def submit(self, *, run_id, tenant_id, project_id=None, run_service=None) -> dict:
        from ros.execution import freestyle_control

        if not freestyle_control.is_enabled():
            # No control service -> behave exactly like local (inline/arq). Keeps dev/tests working.
            logger.info("freestyle backend: ROS_FREESTYLE_SERVICE_URL unset -> local submit for %s", run_id)
            return await super().submit(
                run_id=run_id, tenant_id=tenant_id, project_id=project_id, run_service=run_service
            )
        run_token = self._mint_run_token(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
        receipt = await freestyle_control.dispatch_run(
            run_id=run_id, tenant_id=tenant_id, project_id=project_id,
            master_url=settings.public_base_url, run_token=run_token,
        )
        return {"run_id": run_id, "status": "dispatched", "backend": "freestyle", **receipt}

    def _mint_run_token(self, *, run_id: str, tenant_id: str, project_id: str | None) -> str:
        """Short-lived, run-scoped token the VM presents to master to pull the run manifest
        (scope runtime:pull, run-bound, expiring, revocable)."""
        from ros.security import create_run_token

        return create_run_token(run_id=run_id, tenant_id=tenant_id, project_id=project_id)
