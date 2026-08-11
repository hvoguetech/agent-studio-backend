"""Export a workflow as a runnable LangGraph Studio project (WS8 follow-up).

A workflow already compiles to a real MIT-LangGraph `CompiledStateGraph`
(`ros.engine.compiler.compile_workflow`), so "export to LangGraph" = emit a `langgraph dev`
project whose graph factory reconstructs the graph from the exported executable JSON. The user
runs `langgraph dev` and debugs it in LangGraph Studio locally (step supersteps, inspect/patch
state, time-travel).

Requires the open-core `ros` engine installed locally (public repo). Nodes that need project data
(`tool_call`, `retrieval`) degrade gracefully offline; pure LLM/logic/code graphs run as-is with
provider keys from the environment.
"""

from __future__ import annotations

import io
import json
import re
import zipfile

from ros.services.langgraph_transpile import transpile

_README = '''# {name} — LangGraph Studio export

This is a `langgraph dev` project. `graph.py` is a **readable, explicit LangGraph `StateGraph`**
transpiled from the ROS workflow **{name}** — every node and edge is literal Python you can edit.
`start`/`end`/`transform`/`llm` are inlined; `agent`/`tool_call`/`retrieval`/etc. delegate to the
ROS engine via `_ros()` (their behaviour — agent middleware, materialized tools, RAG — can't be
inlined as plain LangGraph). Run and debug it locally in **LangGraph Studio**.

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Install the open-core ROS engine (public repo):
#   git clone https://github.com/marutsinghhvogue/agent-studio-backend
#   pip install -e agent-studio-backend/apps/api
```

## 2. Configure keys

```bash
cp .env.example .env
# edit .env — set the provider key(s) your workflow's models use
```

## 3. Run

```bash
langgraph dev
```

Opens LangGraph Studio in your browser against this graph (`{graph_key}`). Send an input, step
through supersteps, inspect/patch state, set breakpoints, time-travel.

To run it headless instead: `python graph.py`.

## Notes

- Models resolve from environment keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, …)
  via `init_chat_model`. Set `ROS_DEFAULT_MODEL` for nodes that leave the model blank.
- `llm`, `transform`, `router`, `loop`, `join` run fully. `agent`/`deep_agent` run via `_ros()`
  (fully, unless they call ROS-**materialized tools**). `tool_call`, `retrieval`, and `subworkflow`
  (referencing *other* workflows) need this project's tools / knowledge base / sibling workflows,
  which aren't part of this export — offline they degrade (a "tool not available" result / empty
  retrieval / no-op subworkflow).
- **HITL** (`human_input` / `handoff`) needs a checkpointer to pause/resume: `langgraph dev`
  provides thread persistence; running `python graph.py` uses an in-memory saver (see the file).
- **Not reproduced:** per-node retry / `error_policy` / fanout isolation and node output/input
  schema enforcement are ROS runtime features and are not applied in this exported graph.
- **Versions:** install into a clean virtualenv and let the `ros` install pin `langgraph`/`langchain`
  (this project was exported against a specific langgraph version). Mixing a global env can conflict.
- **Secrets:** `executable.json` (bundled for `subworkflow` refs + as the re-transpile source) is the
  workflow definition verbatim — it should carry secret *references*, not raw keys. Review before sharing.
- **Edit `graph.py` directly** — it's a normal LangGraph `StateGraph`. Or change the workflow in the
  builder and re-export.
'''

_LANGGRAPH_JSON = {
    "dependencies": ["."],
    "graphs": {},  # filled with {graph_key: "./graph.py:make_graph"}
    "env": ".env",
}

_REQUIREMENTS = """# Run this exported workflow with `langgraph dev` (LangGraph Studio):
langgraph-cli[inmem]>=0.2
# Plus the open-core ROS engine that compiles the workflow. It pulls a COMPATIBLE langgraph +
# langchain, so don't pin langgraph here (let ROS own the version). Not on PyPI - install from source:
#   git clone https://github.com/marutsinghhvogue/agent-studio-backend
#   pip install -e agent-studio-backend/apps/api
"""

_ENV_EXAMPLE = """# Provider API keys — read by the model layer (langchain init_chat_model).
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
# Optional: default model for nodes that leave it blank (e.g. openai:gpt-4o-mini, anthropic:claude-sonnet-4-6)
ROS_DEFAULT_MODEL=
"""


def _graph_key(name: str | None, workflow_id: str) -> str:
    """A valid LangGraph graph id from the workflow name/id (identifier-safe, letter-led)."""
    raw = (name or workflow_id or "workflow").strip()
    key = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_") or "workflow"
    if not key[0].isalpha():
        key = f"wf_{key}"
    return key.lower()


def build_files(workflow_id: str, name: str | None, executable: dict) -> dict[str, str]:
    """filename -> file contents for the LangGraph Studio project bundle."""
    display = name or workflow_id or "workflow"
    key = _graph_key(name, workflow_id)
    langgraph_json = dict(_LANGGRAPH_JSON, graphs={key: "./graph.py:make_graph"})
    return {
        "langgraph.json": json.dumps(langgraph_json, indent=2) + "\n",
        "graph.py": transpile(executable or {}, name=display),
        "executable.json": json.dumps(executable or {}, indent=2, sort_keys=False) + "\n",
        "requirements.txt": _REQUIREMENTS,
        ".env.example": _ENV_EXAMPLE,
        "README.md": _README.format(name=display, graph_key=key),
    }


def build_zip(workflow_id: str, name: str | None, executable: dict) -> bytes:
    """Zip the LangGraph Studio project bundle into bytes (deterministic order)."""
    files = build_files(workflow_id, name, executable)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(files):
            zf.writestr(fname, files[fname])
    return buf.getvalue()
