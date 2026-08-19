"""Code tool - run a small user-authored Python function as an agent tool.

Sandboxed with RestrictedPython (AST-level: no dunder access, no eval/exec, guarded
item/attr access) plus an allowlisted importer and a bounded execution wait. This is
the right tool for "compute / reshape / glue" logic that REST/JMESPath can't express.

Security note: RestrictedPython prevents most escapes but does NOT bound CPU/memory or
truly kill a runaway thread. For untrusted multi-tenant code at scale, run via an
isolated executor (subprocess/container or the deep-agent sandbox backend); gate with
`ROS_ENABLE_CODE_TOOLS`. The convention is: define `def main(**kwargs): return ...`
(or assign a top-level `result`).
"""

from __future__ import annotations

import json as _json
from typing import Any

from RestrictedPython import compile_restricted, safe_builtins, utility_builtins
from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_iter_unpack_sequence,
    safer_getattr,
)

from ros.config import settings

# Modules a code tool may import - pure/standard, no IO or network.
_ALLOWED_IMPORTS = {
    "json", "math", "re", "datetime", "statistics", "random", "string",
    "itertools", "functools", "collections", "decimal", "base64", "hashlib", "uuid",
}


# Ceiling on a code tool's returned value so it can't hand the model a multi-megabyte blob (and
# can't be used to amplify memory). Wanted setting: `code_tool_max_result_chars` (default 100000);
# a module constant for now.
_MAX_RESULT_CHARS = 100_000


def cap_result(result: Any, limit: int = _MAX_RESULT_CHARS) -> Any:
    """Return the result unchanged, or a small marker when its serialized size is over `limit`.
    Shared by every executor tier (see ros.tools.sandbox)."""
    try:
        s = result if isinstance(result, str) else _json.dumps(result, default=str)
    except Exception:  # noqa: BLE001 - unserializable -> fall back to repr for the size check
        s = str(result)
    if len(s) > limit:
        return {"error": "result_too_large", "chars": len(s), "limit": limit, "preview": s[:2000]}
    return result


def _guarded_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root not in _ALLOWED_IMPORTS:
        raise ImportError(f"import of {name!r} is not allowed in a code tool")
    return __import__(name, *args, **kwargs)


class _CaptureCollector:
    """A `_print_` collector RestrictedPython instantiates once per scope.

    RestrictedPython rewrites `print(x)` to `_print_()._call_print(x)` and only exposes the text
    via a scope-local `printed` name IF the user code reads it. We don't want to require that:
    instead every collector instance appends to a SHARED buffer (`buffer`), so prints from module
    level AND from inside `main()` are all captured and readable afterwards (see run_code). The
    shared buffer is bound fresh per run in _safe_globals so runs never bleed into each other."""

    def __init__(self, buffer: list, _getattr_=None):  # _getattr_ passed by RestrictedPython
        self._buffer = buffer

    def _call_print(self, *args, **kwargs):
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        self._buffer.append(sep.join(str(a) for a in args) + end)

    def __call__(self):  # `printed` access returns the joined text (RestrictedPython contract)
        return "".join(self._buffer)


def _safe_globals(print_buffer: list) -> dict:
    builtins = dict(safe_builtins)
    builtins.update(utility_builtins)
    builtins["__import__"] = _guarded_import

    def _print_factory(_getattr_=None):
        # Bound to THIS run's buffer; RestrictedPython calls it as `_print_(_getattr_=...)`.
        return _CaptureCollector(print_buffer, _getattr_)

    return {
        "__builtins__": builtins,
        "_getiter_": default_guarded_getiter,
        "_getitem_": default_guarded_getitem,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_getattr_": safer_getattr,
        "_write_": full_write_guard,
        # Capture print() so a code tool's stdout (e.g. test/assert output) is surfaced instead of
        # silently dropped (the old no-op). Bound to this run's buffer.
        "_print_": _print_factory,
    }


class CodeToolError(RuntimeError):
    pass


def run_code(source: str, kwargs: dict[str, Any]) -> Any:
    """Compile + execute the source in a restricted namespace and return the result.

    If the tool printed anything (captured via the shared collector), the return is
    ``{"result": <value>, "stdout": <printed text>}`` so test/assert output is surfaced;
    otherwise the bare return value is passed through unchanged (back-compat)."""
    try:
        # RestrictedPython warns "Prints, but never reads 'printed' variable" because our capture
        # doesn't use its scope-local `printed` mechanism (we collect into a shared buffer instead).
        # That's intentional, so silence the noise rather than spam it on every print()-using tool.
        import warnings

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Line .*: Prints, but never reads 'printed' variable.")
            warnings.filterwarnings("ignore", message="Line None: Prints, but never reads 'printed' variable.")
            byte_code = compile_restricted(source, "<code-tool>", "exec")
    except SyntaxError as e:
        raise CodeToolError(f"compile error: {e}") from e
    print_buffer: list[str] = []
    ns = _safe_globals(print_buffer)
    try:
        exec(byte_code, ns)  # noqa: S102 - sandboxed by RestrictedPython
    except Exception as e:  # noqa: BLE001
        raise CodeToolError(f"load error: {type(e).__name__}: {e}") from e
    # Convention: define `def main(**kwargs)` OR assign a top-level `result`. If BOTH a callable
    # `main` and a top-level `result` are present, the script already invoked main itself (e.g.
    # `result = main()`) - so DON'T call it again (that double-ran the body, doubling stdout).
    main = ns.get("main")
    has_result = "result" in ns
    try:
        if callable(main) and not has_result:
            result = main(**kwargs)
        else:
            result = ns.get("result")
    except Exception as e:  # noqa: BLE001
        raise CodeToolError(f"runtime error: {type(e).__name__}: {e}") from e
    stdout = "".join(print_buffer)
    if stdout:
        return {"result": result, "stdout": stdout}
    return result


async def execute_code(cfg: dict, kwargs: dict) -> Any:
    """Adapter over the pluggable code-executor seam (WS5 5a). Builds a CodeRunRequest and
    dispatches to the executor selected by ROS_CODE_EXECUTOR (default `restricted`, the
    in-process RestrictedPython tier that preserves prior behaviour)."""
    if not settings.enable_code_tools:
        raise CodeToolError("code tools are disabled (ROS_ENABLE_CODE_TOOLS=false)")
    from ros.tools.sandbox import CodeRunRequest, get_code_executor

    req = CodeRunRequest(
        source=cfg.get("source") or "",
        kwargs=kwargs,
        language=cfg.get("language", "python"),
        timeout_s=float(cfg.get("timeout_seconds", settings.code_tool_timeout_seconds)),
        max_result_chars=settings.code_tool_max_result_chars,
        allowed_imports=frozenset(_ALLOWED_IMPORTS),
    )
    result = await get_code_executor().run(req)
    if not result.ok:
        raise CodeToolError(result.error or "code tool failed")
    return result.result


def build_code_tool(cfg: dict, ctx):
    from langchain_core.tools import StructuredTool

    from ros.tools.rest import build_args_schema_from_jsonschema

    args_schema = build_args_schema_from_jsonschema(
        cfg.get("args_schema") or {}, name=f"{cfg.get('name', 'code')}_args"
    )

    async def _call(**kwargs):
        return await execute_code(cfg, kwargs)

    return StructuredTool.from_function(
        coroutine=_call, name=cfg["name"], description=cfg.get("description", ""), args_schema=args_schema,
    )
