"""Runner — compile + drive a workflow from a RunManifest (the standalone runtime's core loop).

`build_graph` rebuilds the CompileContext from the manifest (no master DB) and compiles the workflow;
`run` drives it. The checkpointer defaults to an in-process saver for offline/standalone use; in
production the runtime points it at the injected DATABASE_URL (durable Postgres) so run state
survives a VM restart (Part E).
"""

from __future__ import annotations

from typing import Any

from ros.engine.compiler import compile_workflow
from ros.services.runtime import build_compile_context_from_manifest


def build_graph(manifest: dict, *, checkpointer: Any = None, end_user: dict | None = None,
                run_context: dict | None = None):
    """Manifest → compiled LangGraph graph (context rebuilt without the master DB)."""
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()
    ctx = build_compile_context_from_manifest(
        manifest, checkpointer=checkpointer, end_user=end_user, run_context=run_context,
    )
    return compile_workflow(manifest["executable"], ctx)


async def run(manifest: dict, input: dict, *, thread_id: str = "run", checkpointer: Any = None,
              end_user: dict | None = None, run_context: dict | None = None) -> dict:
    """Compile the manifest's workflow and drive it to completion (or an interrupt), returning the
    final state. Uses a checkpointer so an interrupted (HITL) run can resume on the same thread_id."""
    graph = build_graph(manifest, checkpointer=checkpointer, end_user=end_user, run_context=run_context)
    config = {"configurable": {"thread_id": thread_id}}
    return await graph.ainvoke(input, config)
