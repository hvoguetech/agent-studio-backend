"""Code-tool execution sandbox seam (WS5 5a). docs/design/code-execution-sandbox.md."""

from ros.tools.sandbox.base import CodeExecutor, CodeRunRequest, CodeRunResult, SandboxError
from ros.tools.sandbox.registry import get_code_executor, reset_code_executor

__all__ = [
    "CodeExecutor",
    "CodeRunRequest",
    "CodeRunResult",
    "SandboxError",
    "get_code_executor",
    "reset_code_executor",
]
