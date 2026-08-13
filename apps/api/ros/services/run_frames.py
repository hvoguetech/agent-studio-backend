"""Per-chunk stream mapping: one LangGraph `astream` item -> zero or more SSE frames.

Shared by the master driver (`RunService._drive`) and the standalone runtime driver (a Freestyle
VM, `ros.runtime.driver`) so a run streams IDENTICALLY whichever process drives it. Pure + DB-free:
given (public, node_ids, suppressed_message_nodes) it decides which frames a chunk produces. Each
frame is a plain ``{"event", "data"}`` dict. Activity + lifecycle/terminal frames (run / done /
interrupt / error) are emitted by the caller; this covers the streaming body only.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ros.util.serialize import serialize_stream


def map_chunk_frames(
    ns: Any,
    mode: str,
    chunk: Any,
    *,
    public: bool,
    node_ids: set,
    suppressed_message_nodes: set,
) -> Iterator[dict]:
    """Yield the SSE frame(s) a single (namespace, mode, chunk) item produces (0 or 1), applying
    the public-surface redactions (H5). node_error detail is operator-only here because the public
    branch returns first (matches `_client_error(public=False, ...)`, which passes detail through)."""
    if mode == "tasks" and isinstance(chunk, dict):
        # node_start / node_error expose internal node names + error detail - operator-only (H5).
        if public:
            return
        name = chunk.get("name")
        if name in node_ids and "triggers" in chunk:
            yield {"event": "node_start", "data": {"node": name}}
        elif name in node_ids and chunk.get("error") is not None:
            yield {"event": "node_error", "data": {"node": name, "message": str(chunk.get("error"))}}
        return
    # "updates" carry internal node names + intermediate node state; never public, and for operators
    # skip subgraph-internal updates (ns non-empty).
    if mode == "updates" and (public or ns):
        return
    if mode == "messages":
        # A deep_agent sub-agent streams its OWN answer from a namespace containing a "tools:<id>"
        # segment; suppress it so the supervisor's synthesis is the single visible reply.
        if any(str(seg).startswith("tools:") for seg in (ns or ())):
            return
        msg = chunk[0] if isinstance(chunk, (list, tuple)) and chunk else chunk
        if getattr(msg, "type", "") not in ("ai", "AIMessageChunk"):
            return
        meta = chunk[1] if isinstance(chunk, (list, tuple)) and len(chunk) == 2 else {}
        if (meta or {}).get("langgraph_node") in suppressed_message_nodes:
            return
    data = serialize_stream(mode, chunk)
    # The internal node name rides along on message frames; strip it on the public embed surface.
    if public and mode == "messages" and isinstance(data, dict):
        data.pop("node", None)
    yield {"event": mode, "data": data}
