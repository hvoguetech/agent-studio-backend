"""Trusted-VM run driver: drive a run to completion ON THE VM using the SAME RunService._drive as
master, against the SHARED durable state.

Every frame the driver publishes to its `_RunBroker` is mirrored to the relay bus (ros:run:{id}) by
`_RunBroker.publish`, so master's SSE endpoint relays the VM's live stream to the client (A/C3).
`_drive` finalizes the run's DB row (status / answer / tokens) itself, so the reaper sees a completed
run and late reconnects rebuild the terminal frame from the DB.

This is the chosen trusted-VM path (the VM holds shared DB + Redis + secret-key creds, injected at
provision time), NOT the manifest-pull data-plane split: the VM reads the run + workflow + resolved
secrets straight from the shared DB, so there is zero stream divergence and no re-implemented
finalize. The manifest endpoint + run token remain for the stricter, DB-less isolation option.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ros.runtime.driver")


async def drive_run(
    *,
    run_id: str,
    tenant_id: str,
    project_id: str | None = None,
    public: bool = False,
    run_context: dict | None = None,
    resume: bool = False,
    resume_value: Any = None,
    checkpointer: Any = None,
) -> None:
    """Drive `run_id` to a terminal state on this VM, streaming to the relay bus + finalizing the
    DB. With no explicit checkpointer, uses the durable (shared-Postgres in prod) saver so the run
    is visible to master and resumable across a VM restart."""
    from ros.runtime.runner import _durable_checkpointer
    from ros.services.runs import RunService, _RunBroker

    async def _go(cp: Any) -> None:
        svc = RunService(checkpointer=cp)
        # Publishing to a run_id-bound broker mirrors every frame to the relay bus (increment 1);
        # _execute drives _drive (reads run.input/thread from the DB, streams, finalizes the row),
        # then finishes the broker.
        broker = _RunBroker(run_id)
        await svc._execute(
            run_id=run_id,
            tenant_id=tenant_id,
            project_id=project_id,
            public=public,
            run_context=run_context,
            resume=resume,
            resume_value=resume_value,
            broker=broker,
        )
        log.info("VM drove run %s to a terminal state", run_id)

    if checkpointer is not None:
        await _go(checkpointer)
        return
    async with _durable_checkpointer() as cp:
        await _go(cp)
