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

# NB: this is a literal Python-file template — do NOT str.format() it (it's full of braces).
# The only substitution is the __WORKFLOW_NAME__ sentinel in the docstring (see build_files).
_GRAPH_PY = '''"""LangGraph Studio entry point for the exported ROS workflow: "__WORKFLOW_NAME__".

Reconstructs the workflow's compiled StateGraph via the open-core ROS engine so you can run and
debug it locally with `langgraph dev` (LangGraph Studio). Provider API keys are read from the
environment (.env) by the model layer (langchain init_chat_model).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ros.engine.compiler import compile_workflow
from ros.engine.context import CompileContext

_EXECUTABLE = json.loads((Path(__file__).parent / "executable.json").read_text(encoding="utf-8"))


def make_graph(checkpointer=None):
    """Return the compiled workflow graph. `langgraph dev` calls this and injects its own
    thread persistence, so we leave the checkpointer unset here; pass an InMemorySaver() to run
    the graph standalone (see the __main__ block below)."""
    ctx = CompileContext(
        tenant_id="local",
        project_id="local",
        checkpointer=checkpointer,
        default_model=os.environ.get("ROS_DEFAULT_MODEL"),
        # Resolve a `subworkflow` reference to this same exported workflow (best-effort offline).
        workflows={_EXECUTABLE.get("id") or "workflow": _EXECUTABLE},
    )
    # Offline note: tool_call/retrieval/subworkflow nodes need this project's tools/knowledge/
    # sibling workflows, which aren't exported - they degrade (tool "not available" / empty
    # retrieval / no-op subworkflow). agent/llm/router/transform/loop run fully with provider
    # keys from the environment.
    return compile_workflow(_EXECUTABLE, ctx)


if __name__ == "__main__":  # standalone debug run (no Studio)
    import asyncio

    from langgraph.checkpoint.memory import InMemorySaver

    graph = make_graph(InMemorySaver())
    result = asyncio.run(graph.ainvoke(
        {"messages": [{"role": "user", "content": "hello"}]},
        {"configurable": {"thread_id": "local-1"}},
    ))
    print(json.dumps(str(result), indent=2))
'''

_README = '''# {name} — LangGraph Studio export

This is a `langgraph dev` project that reconstructs the ROS workflow **{name}** as a real
LangGraph graph so you can run and debug it locally in **LangGraph Studio**.

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
- `tool_call`, `retrieval`, and `subworkflow` (referencing *other* workflows) need this project's
  tools / knowledge base / sibling workflows, which are **not** part of this export — offline they
  degrade (a "tool not available" result / empty retrieval / no-op subworkflow).
  `agent`/`llm`/`router`/`transform`/`loop` run fully.
- **HITL** (`human_input` / `handoff`) needs a checkpointer to pause/resume: `langgraph dev`
  provides thread persistence; running `python graph.py` uses an in-memory saver (see the file).
- **Versions:** install into a clean virtualenv and let the `ros` install pin `langgraph`/`langchain`
  (this project was exported against a specific langgraph version). Mixing a global env can conflict.
- **Secrets:** `executable.json` is the workflow definition verbatim — it should carry secret
  *references*, not raw keys. Review it before sharing the bundle.
- The graph is regenerated from `executable.json`; edit that (or re-export) to change the workflow.
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
        "graph.py": _GRAPH_PY.replace("__WORKFLOW_NAME__", display),
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
