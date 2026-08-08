"""Code-tool execution seam (WS5 5a). See docs/design/code-execution-sandbox.md.

`execute_code()` builds a `CodeRunRequest` and dispatches to the selected `CodeExecutor`
(resolved by `ros.tools.sandbox.registry.get_code_executor`). Tiers: `restricted` (in-process
RestrictedPython, NOT isolated - the default), `freestyle` (remote sandbox), or a plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class SandboxError(RuntimeError):
    """Executor-infrastructure failure (misconfig, transport) - distinct from user-code errors,
    which come back as CodeRunResult(ok=False, ...)."""


@dataclass(frozen=True)
class CodeRunRequest:
    """One code-tool execution. `kwargs` (the LLM's tool-call args) is the ONLY data that crosses
    into the sandbox - never api env, secrets, or {{ctx.*}}."""

    source: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    language: str = "python"
    timeout_s: float = 5.0          # wall-clock ceiling
    cpu_s: float | None = None      # CPU-seconds (isolating tiers)
    mem_mb: int | None = None       # address-space cap (isolating tiers)
    max_result_chars: int = 100_000  # serialized-result size cap
    allowed_imports: frozenset[str] = frozenset()
    labels: dict[str, str] = field(default_factory=dict)  # tenant/project/run - accounting only


@dataclass(frozen=True)
class CodeRunResult:
    """Outcome. `ok=False` carries a classified `error`:
    'compile' | 'runtime:<Type>' | 'killed:wall|cpu|mem' | 'unsupported:<lang>' | 'sandbox:<detail>'."""

    ok: bool
    result: Any = None
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)  # tier, wall_ms, cpu_ms, peak_rss_mb, ...


@runtime_checkable
class CodeExecutor(Protocol):
    name: str
    isolating: bool  # True => OS/VM-isolated (bounds CPU/mem, can kill a runaway)

    async def run(self, req: CodeRunRequest) -> CodeRunResult: ...
