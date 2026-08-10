"""Typed tool/node output_schema (C/B1).

Two capabilities on a tool's declared `output_schema` (a JSON Schema of its POST-projection
output):

- `validate_output` - validate a tool's projected output against the schema. Default is
  observe/warn (log + a `tools.output_schema_mismatch` metric); opt-in strict RAISES so the
  tool call fails (composing with a node's on_error).
- `infer_schema` - infer a DRAFT schema from a sample response (a live tool test-run), so an
  author gets a starting point instead of writing one by hand. Uses `genson` when available,
  falling back to a small built-in walker.
"""

from __future__ import annotations

import logging
from typing import Any

import jsonschema

from ros.util.metrics import incr

log = logging.getLogger("ros.tools.output_schema")


class OutputSchemaError(ValueError):
    """Raised in strict mode when a tool's projected output violates its output_schema."""


def validate_output(
    value: Any, schema: dict | None, *, strict: bool = False, name: str = "tool",
    metric: str = "tools.output_schema_mismatch", noun: str = "output", label: str = "output_schema",
) -> str | None:
    """Validate `value` against `schema`.

    Returns None on match or when no schema is declared. On mismatch: strict -> raise
    OutputSchemaError; otherwise observe -> log a warning + bump the `metric` counter and return
    the error message. `noun`/`label` word the message so this serves both output validation
    (default: "output"/"output_schema") and WS8 runtime input validation ("input"/"input_schema").
    A malformed schema is an authoring error, not a data failure, so it's logged and treated as
    "no schema"."""
    if not schema:
        return None
    try:
        jsonschema.validate(value, schema)
        return None
    except jsonschema.SchemaError as e:
        log.warning("%s has an invalid %s; skipping validation: %s", name, label, e)
        return None
    except jsonschema.ValidationError as e:
        msg = e.message
    if strict:
        raise OutputSchemaError(f"{name}: {noun} did not match {label}: {msg}")
    log.warning("%s %s mismatch (observe): %s", name, label, msg)
    incr(metric, detail=name)
    return msg


def infer_schema(sample: Any) -> dict:
    """Infer a DRAFT JSON Schema (types + object keys) from a sample value - a starting point the
    author accepts/edits, not a strict contract. Prefers `genson` (robust merging of
    heterogeneous arrays); falls back to a small walker when it's unavailable."""
    try:
        from genson import SchemaBuilder

        builder = SchemaBuilder()
        builder.add_object(sample)
        schema = builder.to_schema()
        schema.pop("$schema", None)
        return schema
    except Exception:  # noqa: BLE001 - genson optional / odd sample; fall back to the walker
        return _walk(sample)


def _walk(value: Any) -> dict:
    # bool BEFORE int - bool is an int subclass in Python.
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, dict):
        return {"type": "object", "properties": {k: _walk(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "items": _walk(value[0]) if value else {}}
    return {}
