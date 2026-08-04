"""Export the FastAPI OpenAPI schema to apps/api/openapi.json - the canonical, versioned API
contract (A/C13). CI regenerates this and fails on drift, so the committed spec is always current;
the frontend generates its typed client from it. Run: `uv run --extra all python scripts/export_openapi.py`.
"""

from __future__ import annotations

import json
import pathlib
import re

from ros.main import create_app

OUT = pathlib.Path(__file__).resolve().parents[1] / "openapi.json"

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}


def _normalize_operation_ids(schema: dict) -> None:
    """FastAPI derives each operationId from the handler name + HTTP method, but a multi-method
    route (e.g. mcp_rpc) picks its method from an UNORDERED set - so the ids are non-deterministic
    across runs (and collide). Rewrite every operationId to a stable `method_path` slug so the
    exported contract is reproducible (the CI drift check depends on this) and unique per operation."""
    for path, item in (schema.get("paths") or {}).items():
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
        for method, op in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(op, dict) and "operationId" in op:
                op["operationId"] = f"{method.lower()}_{slug}"


def main() -> None:
    schema = create_app().openapi()
    _normalize_operation_ids(schema)
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(schema.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
