"""Runner — compile + drive a workflow from a RunManifest (the DB-less strict-isolation VM mode).

NOTE: this is VM mode 2 (manifest-pull, non-streaming ainvoke) - see ros/runtime/__init__.py. The
DEFAULT VM path FreestyleBackend dispatches is `ros.runtime.driver.drive_run` (trusted-VM, shared-DB,
streaming). This module is retained for the harder-isolation option and is not on the streaming path.

`build_graph` rebuilds the CompileContext from the manifest (no master DB) and compiles the workflow;
`run` drives it. In production the checkpointer points at the SHARED Postgres (ROS_CHECKPOINT_BACKEND
=postgres → ROS_CHECKPOINT_POSTGRES_URL/ROS_DATABASE_URL), so run state is durable across a VM
restart AND visible to master (which reads the same DB). Offline/dev falls back to an in-process saver.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from ros.config import settings
from ros.engine.compiler import compile_workflow
from ros.services.runtime import build_compile_context_from_manifest

log = logging.getLogger("ros.runtime.runner")


def build_graph(manifest: dict, *, checkpointer: Any = None, end_user: dict | None = None,
                run_context: dict | None = None, run_id: str | None = None):
    """Manifest → compiled LangGraph graph (context rebuilt without the master DB)."""
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()
    ctx = build_compile_context_from_manifest(
        manifest, checkpointer=checkpointer, end_user=end_user, run_context=run_context,
        run_id=run_id,
    )
    return compile_workflow(manifest["executable"], ctx)


@asynccontextmanager
async def _durable_checkpointer():
    """The prod checkpointer for the VM runner: a SHARED-Postgres saver (durable + visible to master)
    when ROS_CHECKPOINT_BACKEND=postgres; otherwise an in-process saver (offline/dev)."""
    backend = (settings.checkpoint_backend or "").lower()
    if backend == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        dsn = settings.checkpoint_postgres_url or settings.database_url
        for prefix in ("+asyncpg", "+psycopg", "+psycopg2"):
            dsn = dsn.replace(prefix, "")
        async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
            try:
                await cp.setup()
            except Exception:  # noqa: BLE001 - setup is idempotent
                log.debug("checkpointer setup skipped", exc_info=True)
            yield cp
        return
    from langgraph.checkpoint.memory import InMemorySaver

    yield InMemorySaver()


async def run(manifest: dict, input: dict, *, thread_id: str = "run", checkpointer: Any = None,
              end_user: dict | None = None, run_context: dict | None = None,
              run_id: str | None = None) -> dict:
    """Compile the manifest's workflow and drive it to completion (or an interrupt), returning the
    final state. With no explicit checkpointer, uses the durable (shared-Postgres in prod) saver so an
    interrupted (HITL) run resumes on the same thread_id and master sees the state."""
    config = {"configurable": {"thread_id": thread_id}}
    if checkpointer is not None:
        graph = build_graph(manifest, checkpointer=checkpointer, end_user=end_user, run_context=run_context, run_id=run_id)
        return await graph.ainvoke(input, config)
    async with _durable_checkpointer() as cp:
        graph = build_graph(manifest, checkpointer=cp, end_user=end_user, run_context=run_context, run_id=run_id)
        return await graph.ainvoke(input, config)
