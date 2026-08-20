"""Runtime → master client: pull a RunManifest and post the sandbox's write-back callbacks.

The isolating `sandbox` runtime uses ONLY this HTTP client (run-token-authenticated) to reach master —
it holds no DB/Redis/master-key creds. See design/sandbox-backend-build-plan.md.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger("ros.runtime.client")


def load_manifest_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def fetch_manifest(
    master_url: str, token: str | None, run_id: str,
    *, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET the run's RunManifest from master, authenticated with the run-scoped runtime token."""
    path = f"/v1/runtime/runs/{run_id}/manifest"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    own = client is None
    c = client or httpx.AsyncClient(base_url=master_url.rstrip("/"), timeout=60.0)
    try:
        resp = await c.request("GET", path, headers=headers)
        resp.raise_for_status()
        return resp.json()
    finally:
        if own:
            await c.aclose()


class MasterCallback:
    """Post the sandbox's run frames / status / result back to master over the run token. One long-
    lived httpx client for the run. Frames are best-effort (a relay hiccup must not kill the run);
    /result is the durable terminal write and is awaited."""

    def __init__(self, master_url: str, token: str, run_id: str, *, timeout: float = 30.0) -> None:
        self._base = master_url.rstrip("/")
        self._run_id = run_id
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.AsyncClient(base_url=self._base, timeout=timeout)

    async def frames(self, frames: list[dict]) -> None:
        if not frames:
            return
        try:
            await self._client.post(
                f"/v1/runtime/runs/{self._run_id}/frames", json={"frames": frames}, headers=self._headers,
            )
        except Exception:  # noqa: BLE001 - a relay hiccup must never break the run
            log.debug("frames callback failed for %s", self._run_id, exc_info=True)

    async def status(self, status: str = "running", *, heartbeat: bool = True) -> None:
        try:
            await self._client.post(
                f"/v1/runtime/runs/{self._run_id}/status",
                json={"status": status, "heartbeat": heartbeat}, headers=self._headers,
            )
        except Exception:  # noqa: BLE001 - heartbeat is best-effort; the watchdog tolerates a gap
            log.debug("status callback failed for %s", self._run_id, exc_info=True)

    async def result(self, *, status: str, output: dict | None = None, error: str | None = None,
                     total_tokens: int | None = None, total_cost_usd: float | None = None) -> None:
        # The terminal write: awaited + raises so the CLI exit code reflects a failed finalize.
        resp = await self._client.post(
            f"/v1/runtime/runs/{self._run_id}/result",
            json={"status": status, "output": output, "error": error,
                  "total_tokens": total_tokens, "total_cost_usd": total_cost_usd},
            headers=self._headers,
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
