"""Runtime → master client: pull a RunManifest (or load one from a file for offline runs)."""

from __future__ import annotations

import json
from typing import Any

import httpx


def load_manifest_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def fetch_manifest(
    master_url: str, token: str | None, project_id: str, workflow_id: str,
    *, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET the RunManifest from master, authenticated with the runtime token."""
    path = f"/v1/projects/{project_id}/runtime/workflows/{workflow_id}/manifest"
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
