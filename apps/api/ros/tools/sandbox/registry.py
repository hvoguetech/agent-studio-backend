"""Code-executor resolution. Mirrors ros.execution.registry: `ROS_CODE_EXECUTOR` (default
`restricted`) selects a built-in tier, else a `ros.code_executors` entry-point plugin, imported
LAZILY - so the core never imports a tier it doesn't use (e.g. no httpx path unless `freestyle`)."""

from __future__ import annotations

import logging

from ros.config import settings
from ros.tools.sandbox.base import CodeExecutor

log = logging.getLogger("ros.sandbox")

_executor: CodeExecutor | None = None


def get_code_executor() -> CodeExecutor:
    """Process-wide code executor (resolved once, cached)."""
    global _executor
    if _executor is None:
        _executor = _resolve(settings.code_executor)
    return _executor


def reset_code_executor() -> None:
    """Drop the cached executor (tests / after a settings change)."""
    global _executor
    _executor = None


def _resolve(name: str) -> CodeExecutor:
    key = (name or "restricted").strip().lower()
    if key == "restricted":
        from ros.tools.sandbox.restricted import RestrictedExecutor

        log.info("code executor: restricted (in-process RestrictedPython; NOT OS-isolated)")
        return RestrictedExecutor()
    if key == "freestyle":
        from ros.tools.sandbox.freestyle import FreestyleExecutor

        log.info("code executor: freestyle (remote sandbox)")
        return FreestyleExecutor()

    import importlib.metadata as importlib_metadata

    try:
        eps = importlib_metadata.entry_points(group="ros.code_executors")
    except TypeError:  # pragma: no cover - importlib < 3.10 selection API
        eps = importlib_metadata.entry_points().get("ros.code_executors", [])
    for ep in eps:
        if ep.name == key:
            executor = ep.load()()  # imports ONLY this plugin
            log.info("code executor: %s (plugin %s)", key, ep.value)
            return executor

    raise RuntimeError(
        f"Unknown ROS_CODE_EXECUTOR={key!r}: no built-in tier and no installed "
        f"'ros.code_executors' entry-point named {key!r}."
    )
