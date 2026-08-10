# Typed node I/O (WS8)

**Status:** backend implemented (a + b + c); frontend canvas UI is a follow-up in
`agent-studio-frontend`.

## Problem

Data flows between nodes through the shared LangGraph state (a `TypedDict` of typed channels +
reducers). That backbone is solid, but the *contract* around it was metadata-only:

- Every node type declares typed **ports** (`io_type` ∈ messages/text/json/…), but ports carry
  no data at runtime and the only cross-edge check was a coarse, warning-level `io_type`
  compatibility test (skipped for `any`/`control`/message consumers).
- `NodeInstance.output_schema` existed in the schema ("powers builder `{{…}}` autocomplete +
  mapping validation") but **no code read it** — node outputs were never validated.
- Edges were control-flow only (`source → target` + `condition`/`branches`); there was **no
  field-level mapping** (which output field feeds which input), so cross-node data movement
  relied on shared-state key collisions and per-node config (`input_key`/`output_key`/
  `input_mapping`).
- Nothing verified a producer's outputs satisfy a consumer's required inputs.

WS8 closes that gap with three backward-compatible additions. All new fields are optional;
existing workflows and the full test suite are unaffected, and enforcement defaults to
observe/warn (never a surprise hard failure).

## Executable-JSON contract (the source of truth the frontend adopts)

`packages/schemas/ros/workflow.json`:

- **`NodeInstance.output_schema`** *(existing)* — JSON Schema of the node's **primary output
  value**: the value written to its `output_key` (transform/tool_call/webhook_out/classifier/…),
  its `route_key` (retrieval), or `structured_response` (structured `llm`/`agent`/`deep_agent`).
- **`NodeInstance.output_schema_strict`** *(new, default `false`)* — when `true`, a primary-output
  value that violates `output_schema` **raises** at runtime (composes with
  `error_handling.on_error`); otherwise the mismatch is **observed** (logged + a
  `nodes.output_schema_mismatch` metric) and the run continues.
- **`NodeInstance.input_schema`** *(new)* — JSON Schema of what the node expects to consume (as
  declared state keys). Drives the build-time producer→consumer contract check **and** runtime
  input validation before the node runs.
- **`NodeInstance.input_schema_strict`** *(new, default `false`)* — when `true`, incoming state that
  violates `input_schema` **raises** before the node runs (composes with `error_handling.on_error`);
  otherwise the mismatch is **observed** (logged + `nodes.input_schema_mismatch`) and the node runs.
- **`Edge.mappings`** *(new)* — `[{ "from": <JMESPath>, "to": <state key> }]`. After the source
  node runs, each `from` is evaluated over `{…state, …node_output, "output": node_output}` and the
  result is written to state key `to`. Applies only to plain data edges (ignored on
  router/branch/sub-agent edges).

## (a) Runtime output-schema enforcement — `ros/engine/`

`primary_output_key(node_type, config)` (`node_io.py`) names the state key holding a node's single
structured output (mirrors each factory's write). The compiler
(`compiler.py::compile_workflow`) wraps a node whose instance declares `output_schema` with
`enforce_output_schema(...)`, applied **innermost** so a strict violation surfaces *before* the
`error_handling` wrapper — letting `on_error: continue|default` absorb it. Enforcement reuses the
tool-level `ros/tools/output_schema.py::validate_output` (now parameterized by `metric`), and is
skipped when the node produced nothing that run (the key isn't in its update) or has no primary
output value (a messages-only agent — the validator warns at build time).

**Runtime input validation** (symmetric): a node whose instance declares `input_schema` is wrapped
with `enforce_input_schema(...)`, which validates the incoming state — projected to the keys the
schema mentions, so `additionalProperties:false` schemas don't trip on unrelated channels
(`messages`, loop counters) — *before* the node runs. Observe by default (`nodes.input_schema_mismatch`);
`input_schema_strict` raises pre-run (composing with `on_error`). This also catches bad **entry /
trigger inputs** that no upstream `output_schema` covers.

Wrapping order (inner → outer): `raw node → enforce_output_schema → enforce_input_schema →
[resilient_fanout_child] → [with_error_handling] → fold_edge_mappings`.

## (c) Edge field-mapping — `ros/engine/`

The compiler builds `source_id → mappings` from plain data edges and wraps the source with
`fold_edge_mappings(...)` (applied **outermost**), which merges `apply_edge_mappings(...)` output
into the node's state update. Mapped keys ride shared state to whatever target consumes them next,
so **graph topology is unchanged → the canvas↔executable bijection is preserved** (no synthetic
nodes, no new edge kinds). Mappings are *not* collected from router/fanout sources, `branches`
edges, `subagents` handles, or edges touching a folded sub-agent child.

## (b) Build-time contract — `ros/services/validation.py`

- **Mapping validation:** `to` must be a declared State field (**error** — else the write is
  silently dropped); `from` must be valid JMESPath (**error**); a mapping on a control edge is a
  **warning** (it won't apply).
- **Schema validity:** a malformed `output_schema`/`input_schema` is a **warning** (ignored at
  runtime); an `output_schema` on a node with no primary output value **warns** (enforcement
  skipped).
- **Presence contract (opt-in):** for a node declaring `input_schema`, each `required` input that
  isn't a declared State field is an **error** (the node can never receive it); one that's declared
  but written by no node/mapping is a **warning** (only a trigger/initial input could supply it).
- **Field+type contract (opt-in):** on a mapped edge where the consumer declares `input_schema` and
  the producer declares `output_schema`, each mapping whose `to` is a typed consumer input and whose
  `from` resolves into the producer's output value is type-checked (**error** on a definite
  mismatch; `integer`⊆`number`; anything unresolvable is skipped, so it never false-fires).
- **Plain-edge contract (opt-in):** on a plain edge A→B *without* a mapping, when A declares
  `output_schema`, B declares `input_schema`, and B's input_schema names A's primary **output key**
  (i.e. B consumes that key by shared-state convention), the produced value's type is checked
  against the type B expects there (**error** on mismatch). Fires only on that exact key overlap, so
  it never false-fires on unrelated shared state.
- The coarse `io_type` edge warning is **suppressed when the edge has mappings** (the mapping
  bridges the types).

## Not in scope / follow-ups

- **Frontend (agent-studio-frontend):** edge inspector "Data mapping" rows (`from`/`to`), a node
  output/input schema editor, and *infer schema from a test run* (reuse
  `output_schema.infer_schema`, as tools already do). The JSON contract above is stable for the
  canvas to serialize.
- **Deeper type reconciliation:** array item types, `$ref`, and non-trivial JMESPath (filters,
  projections) are intentionally not resolved by the field-type check today.

## Tests

`apps/api/tests/test_typed_node_io.py` — runtime (mapping copies a field; observe vs strict vs
strict-absorbed-by-on_error) and validator (valid mapping; undeclared/invalid mapping;
control-edge warning; input-schema presence error/warning; field-type mismatch; malformed schema
warning).
