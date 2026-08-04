"""Execution backend seam (A/C12).

A narrow interface so run durability, scheduling, crash-reclaim, and singleton coordination
can be swapped by edition WITHOUT the MIT core importing any cloud/SSPL code. The `local`
backend (MIT) is built-in; other backends resolve from the `ros.execution_backends`
entry-point group (see `registry.py`).
"""

from __future__ import annotations

from ros.execution.base import ExecutionBackend
from ros.execution.registry import get_backend, reset_backend, set_backend

__all__ = ["ExecutionBackend", "get_backend", "set_backend", "reset_backend"]
