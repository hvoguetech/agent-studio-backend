"""WS6.5 - checkpoint TTL retention. The sweep ages out checkpoints for FULLY-expired threads
(all runs terminal + newest run past the horizon) and never touches a thread with a live/resumable
run or a partial conversation that still has runs after the cutoff."""

from __future__ import annotations

from datetime import datetime, timedelta


class _FakeCheckpointer:
    """Records the LangGraph thread ids whose checkpoints were deleted."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


async def test_retention_deletes_only_fully_expired_thread_checkpoints():
    from ros.db.base import SessionLocal
    from ros.models import Project, Run, Thread
    from ros.services.retention import RetentionService

    old = datetime.utcnow() - timedelta(days=30)  # past the 7-day horizon
    async with SessionLocal() as s:
        p = Project(tenant_id="tcp", name="R", slug="r", config={"tracing": {"retention_days": 7}})
        s.add(p)
        await s.flush()
        pid = p.id

        # A: fully expired (all runs terminal + old)         -> checkpoint deleted
        # B: old but has a live (running) run                 -> preserved
        # C: partial - one old + one recent run               -> preserved
        ta = Thread(tenant_id="tcp", project_id=pid, workflow_id="w", lg_thread_id="lgA")
        tb = Thread(tenant_id="tcp", project_id=pid, workflow_id="w", lg_thread_id="lgB")
        tc = Thread(tenant_id="tcp", project_id=pid, workflow_id="w", lg_thread_id="lgC")
        s.add_all([ta, tb, tc])
        await s.flush()

        runs = [
            Run(tenant_id="tcp", project_id=pid, workflow_id="w", thread_id=ta.id, status="done"),
            Run(tenant_id="tcp", project_id=pid, workflow_id="w", thread_id=ta.id, status="error"),
            Run(tenant_id="tcp", project_id=pid, workflow_id="w", thread_id=tb.id, status="running"),
            Run(tenant_id="tcp", project_id=pid, workflow_id="w", thread_id=tc.id, status="done"),  # old
            Run(tenant_id="tcp", project_id=pid, workflow_id="w", thread_id=tc.id, status="done"),  # recent
        ]
        s.add_all(runs)
        await s.flush()
        for r in runs[:4]:  # backdate all but C's second (recent) run
            r.created_at = old
        await s.commit()
        a_id, b_id, c_id = ta.id, tb.id, tc.id

    cp = _FakeCheckpointer()
    counts = await RetentionService.purge_expired(checkpointer=cp)

    assert cp.deleted == ["lgA"], f"only the fully-expired thread's checkpoint should go: {cp.deleted}"
    assert counts["checkpoints"] == 1

    async with SessionLocal() as s:
        assert await s.get(Thread, a_id) is None       # expired thread row removed
        assert await s.get(Thread, b_id) is not None    # live run -> kept
        assert await s.get(Thread, c_id) is not None    # partial (recent run) -> kept


async def test_retention_without_checkpointer_skips_checkpoint_cleanup():
    """Back-compat: purge_expired() with no checkpointer still purges rows and simply doesn't
    touch checkpoints (checkpoints count stays 0)."""
    from ros.db.base import SessionLocal
    from ros.models import Project, Run, Thread
    from ros.services.retention import RetentionService

    old = datetime.utcnow() - timedelta(days=30)
    async with SessionLocal() as s:
        p = Project(tenant_id="tcp2", name="R", slug="r2", config={"tracing": {"retention_days": 7}})
        s.add(p)
        await s.flush()
        t = Thread(tenant_id="tcp2", project_id=p.id, workflow_id="w", lg_thread_id="lgX")
        s.add(t)
        await s.flush()
        run = Run(tenant_id="tcp2", project_id=p.id, workflow_id="w", thread_id=t.id, status="done")
        s.add(run)
        await s.flush()
        run.created_at = old
        await s.commit()
        tid = t.id

    counts = await RetentionService.purge_expired()  # no checkpointer
    assert counts["checkpoints"] == 0
    async with SessionLocal() as s:
        assert await s.get(Thread, tid) is not None  # thread row untouched without a checkpointer
