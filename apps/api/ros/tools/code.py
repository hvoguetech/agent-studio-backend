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


def _safe_globals() -> dict:
    builtins = dict(safe_builtins)
    builtins.update(utility_builtins)
    builtins["__import__"] = _guarded_import
    return {
        "__builtins__": builtins,
        "_getiter_": default_guarded_getiter,
        "_getitem_": default_guarded_getitem,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_getattr_": safer_getattr,
        "_write_": full_write_guard,
        "_print_": lambda *a, **k: None,
    }


class CodeToolError(RuntimeError):
    pass


def run_code(source: str, kwargs: dict[str, Any]) -> Any:
    """Compile + execute the source in a restricted namespace and return the result."""
    try:
        byte_code = compile_restricted(source, "<code-tool>", "exec")
    except SyntaxError as e:
        raise CodeToolError(f"compile error: {e}") from e
    ns = _safe_globals()
    try:
        exec(byte_code, ns)  # noqa: S102 - sandboxed by RestrictedPython
    except Exception as e:  # noqa: BLE001
        raise CodeToolError(f"load error: {type(e).__name__}: {e}") from e
    main = ns.get("main")
    try:
        result = main(**kwargs) if callable(main) else ns.get("result")
    except Exception as e:  # noqa: BLE001
        raise CodeToolError(f"runtime error: {type(e).__name__}: {e}") from e
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
