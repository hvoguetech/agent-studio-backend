"""Export the FastAPI OpenAPI schema to apps/api/openapi.json - the canonical, versioned API
contract (A/C13). CI regenerates this and fails on drift, so the committed spec is always current;
the frontend generates its typed client from it. Run: `uv run --extra all python scripts/export_openapi.py`.
"""

from __future__ import annotations

import json
import pathlib

from ros.main import create_app

OUT = pathlib.Path(__file__).resolve().parents[1] / "openapi.json"


def main() -> None:
    schema = create_app().openapi()
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(schema.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
