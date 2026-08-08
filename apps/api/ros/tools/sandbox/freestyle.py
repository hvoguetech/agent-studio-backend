"""Isolating code executor: run the code tool in a remote Freestyle sandbox.

Freestyle (freestyle.sh) runs the code in an isolated VM with CPU/memory bounds and can kill a
runaway - so this tier is safe for untrusted/multi-tenant code, unlike `restricted`.

NOTE ON THE WIRE FORMAT: Freestyle exposes Python execution via its VM API; the exact REST path
and response shape vary by plan/SDK version, so they are CONFIGURABLE (`ROS_FREESTYLE_BASE_URL` +
`ROS_FREESTYLE_RUN_PATH`) rather than hard-coded. Result extraction does NOT depend on Freestyle's
own result-capture: the wrapper prints a `__ROS_RESULT__<json>` sentinel to stdout and we parse
that from whatever stdout/logs field the response carries. Confirm the path/auth for your account
at dash.freestyle.sh; the request/response mapping here is unit-tested against a mocked client.
"""

from __future__ import annotations

import json
from typing import Any

from ros.config import settings
from ros.tools.sandbox.base import CodeRunRequest, CodeRunResult, SandboxError

_SENTINEL = "__ROS_RESULT__"


def _wrap(source: str, kwargs: dict[str, Any]) -> str:
    """User source + a bootstrap that calls main(**kwargs) (or reads top-level `result`) and
    prints the JSON result behind a sentinel. kwargs are embedded as a literal (no stdin needed)."""
    kwargs_json = json.dumps(kwargs)
    return (
        "import json as __rj\n"
        f"{source}\n"
        f"__kw = __rj.loads({kwargs_json!r})\n"
        "__main = globals().get('main')\n"
        "__res = __main(**__kw) if callable(__main) else globals().get('result')\n"
        f"print({_SENTINEL!r} + __rj.dumps(__res, default=str))\n"
    )


def _extract_stdout(payload: dict) -> str:
    """Best-effort stdout text across response shapes (stdout / logs list / result str)."""
    for key in ("stdout", "output", "logs"):
        v = payload.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return "\n".join(
                (item.get("message") or item.get("line") or "") if isinstance(item, dict) else str(item)
                for item in v
            )
    return str(payload.get("result", ""))


def _parse_result(stdout: str):
    for line in reversed(stdout.splitlines()):
        if line.startswith(_SENTINEL):
            return json.loads(line[len(_SENTINEL):])
    raise ValueError("no result sentinel in sandbox stdout")


class FreestyleExecutor:
    name = "freestyle"
    isolating = True

    async def run(self, req: CodeRunRequest) -> CodeRunResult:
        if req.language != "python":
            return CodeRunResult(ok=False, error=f"unsupported:{req.language} (freestyle runs python only)")
        if not settings.freestyle_api_key:
            raise SandboxError("ROS_FREESTYLE_API_KEY is not set (required for code_executor='freestyle')")

        import httpx

        url = settings.freestyle_base_url.rstrip("/") + settings.freestyle_run_path
        body = {
            "language": "python",
            "code": _wrap(req.source, req.kwargs),
            # Freestyle timeout is ms in current SDKs; send a small margin over our wall budget.
            "config": {"timeout": int((req.timeout_s + 1) * 1000)},
        }
        headers = {"Authorization": f"Bearer {settings.freestyle_api_key}"}
        try:
            async with httpx.AsyncClient(timeout=req.timeout_s + 10) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise SandboxError(f"freestyle transport error: {e}") from e
        if resp.status_code >= 400:
            raise SandboxError(f"freestyle HTTP {resp.status_code}: {resp.text[:300]}")

        payload = resp.json()
        status = payload.get("statusCode", payload.get("exit_code", 0))
        stdout = _extract_stdout(payload)
        if status not in (0, None):
            stderr = payload.get("stderr") or payload.get("error") or stdout
            return CodeRunResult(ok=False, error=f"runtime:{str(stderr)[:500]}", metrics={"tier": "freestyle"})
        try:
            result = _parse_result(stdout)
        except (ValueError, json.JSONDecodeError):
            return CodeRunResult(
                ok=False, error=f"runtime:no result ({stdout[:300]})", metrics={"tier": "freestyle"}
            )
        from ros.tools.code import cap_result

        return CodeRunResult(
            ok=True, result=cap_result(result, req.max_result_chars), metrics={"tier": "freestyle"}
        )
