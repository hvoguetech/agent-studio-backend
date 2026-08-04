"""The `ExecutionBackend` interface (A/C12, Doc §3.1).

A deliberately NARROW seam covering only what differs by edition:
- `submit`           - durably execute a NON-interactive run (webhook/schedule/email/eval/MCP).
- `retry`            - operator retry of a terminal run (`resume` | `restart`; see A/C11 #25).
- `reclaim_orphans`  - detect + recover runs whose driver died (extended by A/C9 #23).
- `run_scheduler_tick` - fire due schedule/app_event triggers once (the core keeps the timer and
                       calls this each tick; resolves the spec's open question on scheduling).
- `singleton`        - a leader/singleton gate for periodic sweeps (reaper/retention).

Interactive SSE runs are NOT part of this seam - they stay in `RunService.stream`/`_drive` in
every edition. The shared, backend-agnostic resume primitive lives in `RunService`
(`_continue_from_checkpoint`); both `LocalBackend` (#23) and cloud backends (#36) call it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Any


class ExecutionBackend(ABC):
    """Edition-swappable execution/durability seam. Resolve the active one via
    `ros.execution.get_backend()`; never import a concrete backend directly from the core."""

    #: short backend name (e.g. "local"); used in logs/telemetry.
    name: str = "base"

    async def startup(self, app: Any) -> None:  # noqa: B027 - optional lifecycle hook
        """Bind app state / open resources. Called once from the app lifespan. Default: no-op."""

    async def shutdown(self) -> None:  # noqa: B027 - optional lifecycle hook
        """Release resources. Called once on shutdown. Default: no-op."""

    @abstractmethod
    async def submit(
        self, *, run_id: str, tenant_id: str, project_id: str | None = None,
        run_service: Any = None,
    ) -> dict:
        """Durably execute a non-interactive run. Returns the run result dict (inline) or an
        enqueue receipt (offloaded). `run_service` is an optional adapter the core passes so a
        backend can reuse the caller's RunService (and its checkpointer) for an inline run;
        backends that own execution elsewhere (e.g. Inngest) ignore it."""

    @abstractmethod
    async def retry(
        self, *, run_id: str, tenant_id: str, mode: str, project_id: str | None = None,
        run_service: Any = None,
    ) -> dict:
        """Operator retry of a terminal run. `mode` is "resume" (continue from the last
        checkpoint via the core primitive) or "restart" (fresh run on the latest version).
        The full behavior is delivered by A/C11 (#25); the seam only fixes the contract."""

    @abstractmethod
    async def reclaim_orphans(self) -> int:
        """Detect + recover runs whose driver died. Returns how many were handled. LocalBackend
        wraps today's stale-run reaper; A/C9 (#23) extends it with lease-based reclaim."""

    @abstractmethod
    async def run_scheduler_tick(self) -> int:
        """Fire every due schedule / app_event trigger once. Returns how many fired. The core
        owns the timer and calls this each tick; a cron-driven backend may no-op."""

    @abstractmethod
    def singleton(self, name: str, *, ttl_seconds: int = 120) -> AbstractAsyncContextManager:
        """A leader/singleton gate for periodic sweeps (reaper/retention). Yields True to the
        single holder and False to everyone else, so a multi-replica deployment runs the sweep
        once. A/C2 (#4) builds full leader-election on top of this."""
