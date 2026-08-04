"""Backend resolution (A/C12, Doc §3.2).

`ROS_EXECUTION_BACKEND` (default `local`) selects the backend. `local` is the built-in MIT
`LocalBackend`; any other value is resolved from the `ros.execution_backends` entry-point
group and imported LAZILY - so the core never imports a plugin that isn't selected/installed,
and an all-MIT install never touches cloud/SSPL code.
"""

from __future__ import annotations

import logging

from ros.config import settings
from ros.execution.base import ExecutionBackend

log = logging.getLogger("ros.execution")

_backend: ExecutionBackend | None = None


def get_backend() -> ExecutionBackend:
    """Return the process-wide execution backend (resolved once, cached)."""
    global _backend
    if _backend is None:
        _backend = _resolve(settings.execution_backend)
    return _backend


def _resolve(name: str) -> ExecutionBackend:
    key = (name or "local").strip().lower()
    if key == "local":
        from ros.execution.local import LocalBackend  # built-in, MIT

        log.info("execution backend: local")
        return LocalBackend()

    # Plugin backends resolve from the entry-point group; ONLY the selected one is imported.
    import importlib.metadata as importlib_metadata

    try:
        eps = importlib_metadata.entry_points(group="ros.execution_backends")
    except TypeError:  # pragma: no cover - importlib < 3.10 selection API
        eps = importlib_metadata.entry_points().get("ros.execution_backends", [])
    for ep in eps:
        if ep.name == key:
            backend = ep.load()()  # imports ONLY this plugin
            log.info("execution backend: %s (plugin %s)", key, ep.value)
            return backend

    raise RuntimeError(
        f"Unknown ROS_EXECUTION_BACKEND={key!r}: no built-in backend and no installed "
        f"'ros.execution_backends' entry-point named {key!r}."
    )


def set_backend(backend: ExecutionBackend | None) -> None:
    """Override the resolved backend (tests / embedding)."""
    global _backend
    _backend = backend


def reset_backend() -> None:
    """Clear the cached backend so the next `get_backend()` re-resolves (tests)."""
    global _backend
    _backend = None
