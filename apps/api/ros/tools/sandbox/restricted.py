"""Default code executor: in-process RestrictedPython (AST hardening).

NOT OS-isolated - no CPU/memory bound, and on wall-clock timeout the worker thread cannot be
killed (it runs to completion on the shared executor). Fine for trusted single-tenant; for
untrusted/multi-tenant code use an isolating tier (freestyle/plugin). Preserves the exact
pre-seam behaviour of `execute_code`.
"""

from __future__ import annotations

import asyncio

from ros.tools.sandbox.base import CodeRunRequest, CodeRunResult


class RestrictedExecutor:
    name = "restricted"
    isolating = False

    async def run(self, req: CodeRunRequest) -> CodeRunResult:
        if req.language != "python":
            return CodeRunResult(ok=False, error=f"unsupported:{req.language} (restricted runs python only)")
        # Lazy import keeps the registry import light and avoids any import cycle with code.py.
        from ros.tools.code import CodeToolError, cap_result, run_code

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(run_code, req.source, req.kwargs), timeout=req.timeout_s
            )
        except TimeoutError:
            # The awaited result is abandoned cleanly, but the CPython thread keeps running - the
            # documented DoS residual of this non-isolating tier.
            return CodeRunResult(
                ok=False,
                error=f"killed:wall (timed out after {req.timeout_s}s; restricted tier cannot kill the thread)",
                metrics={"tier": "restricted"},
            )
        except CodeToolError as e:
            return CodeRunResult(ok=False, error=f"runtime:{e}", metrics={"tier": "restricted"})
        return CodeRunResult(
            ok=True, result=cap_result(raw, req.max_result_chars), metrics={"tier": "restricted"}
        )
