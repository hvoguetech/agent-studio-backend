# Enhancement — Infer node schemas from a test run (WS8 follow-up)

**Status:** proposed / not started. **Area:** workflow builder + engine (typed node I/O).
**Related:** [`docs/design/typed-node-io.md`](../design/typed-node-io.md), WS8 in
[`docs/improvement-plan-2026-08.md`](../improvement-plan-2026-08.md).

## Problem

WS8 lets a node declare `output_schema` / `input_schema` and constrains generation via
`response_format`, but authors still hand-write those JSON Schemas. Tools already solve this on a
tool test-run (`ros/tools/output_schema.py::infer_schema` — genson-backed, with a walker fallback;
surfaced when a tool is test-run). Nodes have no equivalent: there's no "run it once, then use the
observed shape as a starting schema."

## Proposal

After a workflow **test run**, let the builder infer a **draft** JSON Schema for a node from the
values it actually produced/consumed, and offer to fill `output_schema` / `input_schema` with it
(author accepts/edits — a draft, not a hard contract). Reuse `infer_schema` so tool and node
inference behave identically.

### Where the sample comes from
The `WorkflowTestPanel` already streams per-node run debug (`onNodeStep` / `onFinalDebug` in
`components/screens/workflows.tsx`). Two implementation options:

1. **Client-side (lighter).** If the run debug already carries each node's output value (the value
   at `primaryOutputKey`) and/or its inbound state, infer the draft in TS (a small walker mirroring
   `infer_schema`'s fallback) and show an **"Infer from last run"** button next to the
   output/input schema editors. No backend change; limited to whatever the debug frame exposes.
2. **Backend endpoint (robust).** Add `POST /v1/projects/{id}/workflows/{wf}/nodes/{node}/infer-schema`
   that takes a captured sample (or re-derives it from the last run's checkpoint/trace) and returns
   `infer_schema(sample)` for `output` and, from the node's inbound state projection, for `input`.
   Handles heterogeneous arrays well (genson) and keeps parity with the tool path.

**Recommended:** start with (1) if the debug frame already exposes node output values; fall back to
(2) if it doesn't (a `structured_response` / `primaryOutputKey` value must be captured per node).

### UX
- **Output:** on `llm`/`agent` (Structured JSON output) and data nodes (Output schema box) — an
  "Infer from last run" affordance that fills the schema textarea with the drafted schema.
- **Input:** on the data-node Input-schema editor, alongside the existing **"Infer from
  <upstream>"** button (which infers from the upstream *declared* schema); this new one infers from
  the upstream node's *observed* output in the last run when no declared schema exists.
- Always a **draft**: populate the editor, let the author edit, never auto-enforce.

## Non-goals
- No automatic enforcement of inferred schemas (draft only).
- No deep/array `$ref` reconciliation (tracked separately under WS8 "Later").

## Effort
S–M. (1) is S (client walker + button, needs the debug frame to carry node values). (2) is M (one
endpoint + sample capture from the run/checkpoint). Reuses `infer_schema`; no migration.
