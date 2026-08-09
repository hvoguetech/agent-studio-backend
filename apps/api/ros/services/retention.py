"""RetentionService - scheduled data purge (finding e).

Honors per-project `config.tracing.retention_days` for traces/spans/runs (falling back to
`settings.default_trace_retention_days`) and a workspace-wide `settings.audit_log_retention_days`
for audit logs. 0 / unset anywhere = keep forever (no purge). Wired into the leader-only reaper
loop in main.py; safe to run repeatedly (idempotent, time-based).

Each project's deletes run inside `tenant_guard(tenant_id)` so the Postgres RLS GUC
(`app.current_tenant`) is set and the tenant-isolation policies match - a purge that ran with no
tenant bound would delete nothing on Postgres (fail-closed) and everything it shouldn't nowhere.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy import delete as sa_delete

from ros.config import settings
from ros.db.base import SessionLocal
from ros.db.scoping import tenant_guard
from ros.models import AuditLog, Project, Run, Span, Thread, Trace

log = logging.getLogger("ros.retention")

# Run states that are NOT terminal - a thread with any of these has a live/resumable run, so its
# checkpoint must NOT be deleted (it may still be driven/resumed).
_NONTERMINAL_RUN_STATES = ("queued", "running", "interrupted")

# Cap the traces processed per project per sweep so one huge backlog can't build an unbounded
# DELETE. The sweep runs on a timer, so it drains a backlog over successive passes.
_TRACE_BATCH = 20_000


def _project_retention_days(project: Project) -> int:
    cfg = project.config or {}
    days = (cfg.get("tracing") or {}).get("retention_days")
    if days is None:
        days = settings.default_trace_retention_days
    try:
        return int(days or 0)
    except (TypeError, ValueError):
        return 0


class RetentionService:
    @staticmethod
    async def purge_expired(checkpointer: Any = None) -> dict[str, int]:
        """Delete traces/spans/runs past each project's retention horizon and audit logs past
        the workspace-wide horizon. When `checkpointer` is provided, also age out LangGraph
        checkpoints for fully-expired threads (WS6.5). Returns per-entity deleted counts. Never
        raises (logs and continues) so a purge failure can't take down the scheduler loop."""
        counts = {"traces": 0, "spans": 0, "runs": 0, "checkpoints": 0, "audit_logs": 0}
        if not settings.enable_retention:
            return counts
        try:
            async with SessionLocal() as s:
                projects = (await s.execute(select(Project))).scalars().all()
        except Exception:  # noqa: BLE001
            log.exception("retention: failed to list projects")
            return counts

        for project in projects:
            days = _project_retention_days(project)
            if days <= 0:
                continue
            cutoff = datetime.utcnow() - timedelta(days=days)
            try:
                s, r, ru, cp = await RetentionService._purge_project(
                    project.tenant_id, project.id, cutoff, checkpointer
                )
                counts["spans"] += s
                counts["traces"] += r
                counts["runs"] += ru
                counts["checkpoints"] += cp
            except Exception:  # noqa: BLE001 - one bad project must not abort the sweep
                log.exception("retention: purge failed for project %s", project.id)

        counts["audit_logs"] += await RetentionService._purge_audit_logs(projects)
        if any(counts.values()):
            log.info("retention purge removed %s", counts)
        return counts

    @staticmethod
    async def _purge_project(
        tenant_id: str, project_id: str, cutoff, checkpointer: Any = None
    ) -> tuple[int, int, int, int]:
        """Purge one project's expired spans/traces/runs (and, if a checkpointer is given,
        checkpoints for fully-expired threads). `tenant_guard` (sync CM) binds the RLS GUC for the
        whole DB session so the Postgres tenant-isolation policies match."""
        with tenant_guard(tenant_id):
            async with SessionLocal() as s:
                # Fully-expired threads (WS6.5): every run terminal AND newest run past the cutoff.
                # A thread with a live run (queued/running/interrupted), or a partial conversation
                # with any run after the cutoff, is excluded so we never drop a resumable checkpoint.
                expired_thread_ids: list[str] = []
                if checkpointer is not None:
                    expired_thread_ids = (
                        await s.execute(
                            select(Run.thread_id)
                            .where(Run.tenant_id == tenant_id, Run.project_id == project_id)
                            .group_by(Run.thread_id)
                            .having(func.max(Run.created_at) < cutoff)
                            .having(
                                func.sum(
                                    case((Run.status.in_(_NONTERMINAL_RUN_STATES), 1), else_=0)
                                )
                                == 0
                            )
                        )
                    ).scalars().all()

                trace_ids = (
                    await s.execute(
                        select(Trace.id).where(
                            Trace.tenant_id == tenant_id,
                            Trace.project_id == project_id,
                            Trace.created_at < cutoff,
                        ).limit(_TRACE_BATCH)
                    )
                ).scalars().all()
                spans = traces = 0
                if trace_ids:
                    spans = (await s.execute(sa_delete(Span).where(Span.trace_id.in_(trace_ids)))).rowcount or 0
                    traces = (await s.execute(sa_delete(Trace).where(Trace.id.in_(trace_ids)))).rowcount or 0
                runs = (
                    await s.execute(
                        sa_delete(Run).where(
                            Run.tenant_id == tenant_id,
                            Run.project_id == project_id,
                            Run.created_at < cutoff,
                        )
                    )
                ).rowcount or 0

                # Resolve the LangGraph thread keys for the expired threads, drop the Thread rows,
                # commit the DB deletes, THEN delete the checkpoints (external store; best-effort).
                lg_thread_ids: list[str] = []
                if expired_thread_ids:
                    lg_thread_ids = (
                        await s.execute(
                            select(Thread.lg_thread_id).where(
                                Thread.tenant_id == tenant_id, Thread.id.in_(expired_thread_ids)
                            )
                        )
                    ).scalars().all()
                    await s.execute(
                        sa_delete(Thread).where(
                            Thread.tenant_id == tenant_id, Thread.id.in_(expired_thread_ids)
                        )
                    )
                await s.commit()

        checkpoints = 0
        for lg in lg_thread_ids:
            try:
                if hasattr(checkpointer, "adelete_thread"):
                    await checkpointer.adelete_thread(lg)
                elif hasattr(checkpointer, "delete_thread"):
                    checkpointer.delete_thread(lg)
                checkpoints += 1
            except Exception:  # noqa: BLE001 - the run/thread rows are already gone; best-effort
                log.debug("retention: checkpoint delete failed for thread %s", lg, exc_info=True)
        return spans, traces, runs, checkpoints

    @staticmethod
    async def _purge_audit_logs(projects: list[Project]) -> int:
        """Age out audit logs past `audit_log_retention_days` (0 = keep forever). Audit rows are
        immutable (no UPDATE; see infra/postgres_rls.sql) - this bulk time-based purge is the one
        sanctioned removal path. Runs per-tenant so the RLS GUC matches on Postgres."""
        days = int(settings.audit_log_retention_days or 0)
        if days <= 0:
            return 0
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = 0
        for tenant_id in {p.tenant_id for p in projects}:
            try:
                with tenant_guard(tenant_id):
                    async with SessionLocal() as s:
                        deleted += (
                            await s.execute(
                                sa_delete(AuditLog).where(
                                    AuditLog.tenant_id == tenant_id, AuditLog.created_at < cutoff
                                )
                            )
                        ).rowcount or 0
                        await s.commit()
            except Exception:  # noqa: BLE001
                log.exception("retention: audit purge failed for tenant %s", tenant_id)
        return deleted
