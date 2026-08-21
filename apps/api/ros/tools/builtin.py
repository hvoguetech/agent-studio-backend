"""First-party builtin tools: current_time, calculator, web_fetch, web_search,
knowledge_search (agent-callable RAG over the project knowledge base)."""

from __future__ import annotations

import ast
import operator as op
from datetime import UTC, datetime

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# Built-in tools every project gets by default (platform capabilities). They are provisioned
# automatically (on project create + whenever the tools list is read), are protected from
# deletion, and are never carried in import/export bundles - so importing a project neither
# duplicates nor loses them. `builtin` is both the tool name the model calls and the config key.
BUILTIN_DEFAULTS: list[dict[str, str]] = [
    {"builtin": "current_time", "description": "Return the current date and time."},
    {"builtin": "calculator", "description": "Evaluate an arithmetic expression safely."},
    {"builtin": "web_fetch", "description": "Fetch the contents of a URL."},
    {"builtin": "web_search", "description": "Search the web (requires a configured search-provider key)."},
    {"builtin": "knowledge_search", "description": "Search this project's knowledge base (docs + Q&A)."},
    {"builtin": "remember", "description": "Store a durable memory for later recall."},
    {"builtin": "recall", "description": "Recall stored memories relevant to a query."},
]

# Safe arithmetic for the calculator (no names, no calls).
_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.Pow: op.pow, ast.Mod: op.mod, ast.USub: op.neg, ast.FloorDiv: op.floordiv,
}

# Bound exponentiation so an expression like `9**9**9` can't build a multi-gigabyte int and
# lock the interpreter (DoS). Ordinary calculator use stays well within these limits.
_MAX_POW_EXPONENT = 100
_MAX_POW_BASE = 1_000_000


def _calc(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _calc(node.left), _calc(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > _MAX_POW_EXPONENT or abs(left) > _MAX_POW_BASE):
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_calc(node.operand))
    raise ValueError("Unsupported expression")


class _CalcArgs(BaseModel):
    expression: str = Field(description="Arithmetic expression, e.g. '2 * (3 + 4)'")


class _TimeArgs(BaseModel):
    tz: str = Field(default="UTC", description="Timezone name (only UTC supported offline)")


class _FetchArgs(BaseModel):
    url: str = Field(description="URL to fetch")


def _memory_scope(ctx) -> str:
    """Per-end-user long-term-memory partition. Without this every end user of a deployed
    multi-user workflow shares one project-global memory pool, so user A's remembered
    facts (preferences, account details) are recalled for user B - a privacy leak. Falls
    back to 'default' for anonymous / internal (operator) runs, preserving prior behavior
    for single-user setups."""
    eu = getattr(ctx, "end_user", None) or {}
    uid = str(eu.get("id") or "").strip()
    return f"user:{uid}" if uid else "default"


def build_builtin_tool(cfg: dict, ctx):
    builtin = cfg["builtin"]
    name = cfg.get("name", builtin)
    desc = cfg.get("description", "")

    if builtin == "current_time":
        def now(tz: str = "UTC") -> str:
            return datetime.now(UTC).isoformat()
        return StructuredTool.from_function(func=now, name=name, description=desc or "Get the current UTC time.", args_schema=_TimeArgs)

    if builtin == "calculator":
        def calc(expression: str) -> str:
            return str(_calc(ast.parse(expression, mode="eval").body))
        return StructuredTool.from_function(func=calc, name=name, description=desc or "Evaluate an arithmetic expression.", args_schema=_CalcArgs)

    if builtin == "web_fetch":
        async def fetch(url: str) -> str:
            from ros.util.http import shared_async_client
            from ros.util.ssrf import guarded_get
            r = await guarded_get(
                shared_async_client(), url, policy=getattr(ctx, "egress_policy", None),
                timeout=20, follow_redirects=True,
            )
            return r.text[:8000]
        return StructuredTool.from_function(coroutine=fetch, name=name, description=desc or "Fetch a URL and return its text.", args_schema=_FetchArgs)

    if builtin == "web_search":  # requires a provider key (Tavily/Exa) - wired in Phase 7
        def search(query: str) -> str:
            return "web_search is not configured. Add a Tavily/Exa key to enable it."
        class _Q(BaseModel):
            query: str = Field(description="Search query")
        return StructuredTool.from_function(func=search, name=name, description=desc or "Search the web.", args_schema=_Q)

    if builtin == "knowledge_search":
        # Retrieval as a TOOL (vs the fixed `retrieval` node): the agent can search per
        # sub-question, multiple times, with its own phrasing - which is what makes a
        # single agent handle multi-part questions instead of a classifier→router
        # picking one path.
        class _KSearchArgs(BaseModel):
            query: str = Field(description="What to look up in the project knowledge base. Use one focused query per sub-question.")
            folder: str = Field(default="", description="Optional knowledge folder to search within (empty = all folders)")
            top_k: int = Field(default=4, description="How many document chunks to return")

        async def ksearch(query: str, folder: str = "", top_k: int = 4) -> str:
            from ros.db.base import SessionLocal
            from ros.knowledge.embeddings import DEFAULT_MIN_SCORE
            from ros.knowledge.store import citation_for
            from ros.services.knowledge import KnowledgeService

            async with SessionLocal() as s:
                embedder = await KnowledgeService.embedder_for_project(s, ctx.tenant_id, ctx.project_id)
                vec = await embedder.aembed_query(query)
                try:
                    hits = await KnowledgeService.search(
                        s, ctx.tenant_id, ctx.project_id, query, top_k=top_k,
                        folders=[folder] if folder else None, embedder=embedder, embedding=vec,
                    )
                except Exception:  # noqa: BLE001 - store empty / not ready
                    hits = []
                # Vector-only path here, so Hit.score IS the cosine; floor at the calibrated
                # default so an off-topic query returns nothing (agent then says it doesn't know).
                hits = [h for h in hits if h.score >= DEFAULT_MIN_SCORE]
                try:
                    qa = await KnowledgeService.top_qa(
                        s, ctx.tenant_id, ctx.project_id, query, top_k=3, threshold=0.3,
                        embedder=embedder, embedding=vec,
                    )
                except Exception:  # noqa: BLE001
                    qa = []
            blocks = [f"[Doc {i + 1}{f' · {c}' if (c := citation_for(h.metadata)) else ''} · score {h.score:.2f}] {h.text}"
                      for i, h in enumerate(hits)]
            blocks += [f"[FAQ] Q: {q['question']}\nA: {q['answer']}" for q in qa]
            return "\n\n".join(blocks) if blocks else "No relevant knowledge found for this query."

        return StructuredTool.from_function(
            coroutine=ksearch, name=name,
            description=desc or "Search the project knowledge base (documents + FAQs). Call once per distinct sub-question.",
            args_schema=_KSearchArgs,
        )

    if builtin == "remember":
        class _RememberArgs(BaseModel):
            text: str = Field(description="A concise fact worth remembering for future conversations (e.g. a preference, a decision, an account detail).")

        async def remember_tool(text: str) -> str:
            from ros.db.base import SessionLocal
            from ros.services.memory import MemoryService

            async with SessionLocal() as s:
                await MemoryService.remember(s, ctx.tenant_id, ctx.project_id, text, scope=_memory_scope(ctx))
            return "Saved to long-term memory."

        return StructuredTool.from_function(
            coroutine=remember_tool, name=name,
            description=desc or "Save a fact to long-term memory so it persists across conversations.",
            args_schema=_RememberArgs,
        )

    if builtin == "recall":
        class _RecallArgs(BaseModel):
            query: str = Field(description="What to look up in long-term memory.")

        async def recall_tool(query: str) -> str:
            from ros.db.base import SessionLocal
            from ros.services.memory import MemoryService

            async with SessionLocal() as s:
                mems = await MemoryService.recall(s, ctx.tenant_id, ctx.project_id, query, scope=_memory_scope(ctx), top_k=5)
            return "\n".join(f"- {m}" for m in mems) if mems else "No relevant memories found."

        return StructuredTool.from_function(
            coroutine=recall_tool, name=name,
            description=desc or "Recall previously remembered facts from long-term memory.",
            args_schema=_RecallArgs,
        )

    if builtin == "provision_resource":
        # Agent self-provisioning (#6 slice 2): let an agent, mid-run, provision an ISOLATED resource
        # stack for the end user it is currently serving (JIT isolation on first contact). GATED here
        # (builtins bypass the materialize entitlement wrapper) on the run's GOVERNED SUBJECT —
        # ctx.agent_id, the ApiKey the run acts as: its default-deny `backend:provision` capability
        # allow-list + per-subject capacity cap. Scoped to (agent_id, ctx.end_user) so each end user
        # gets their OWN resources (the forUser model); the resolved env is injected into this
        # subject's subsequent runs (see runtime_env / per-end-user isolation 2b). Operator/console
        # runs (no governed subject) cannot self-provision from inside a run — they use the HTTP route.
        _PROVISION_CAP = "backend:provision"

        class _ProvisionArgs(BaseModel):
            template: str = Field(default="db", description="Which starter stack to provision for the current end user: 'db' (Postgres), 'db+storage' (Postgres + object storage), or 'db+storage+queue' (full stack).")
            kind: str = Field(default="", description="Optional: provision a single provider kind instead of a template (e.g. 'railway-postgres', 'railway-storage', 'queue').")
            resource_name: str = Field(default="", description="Optional human-readable name for the provisioned resource(s).")

        async def provision_resource_tool(template: str = "db", kind: str = "", resource_name: str = "") -> str:
            import json

            from sqlalchemy import select

            from ros.db.base import SessionLocal
            from ros.models.entities import ApiKey
            from ros.services import backend_provisioning as bp
            from ros.services import provision_templates as templates
            from ros.services.apikeys import ApiKeyService
            from ros.services.budget import ProvisionNotAllowed

            gid = getattr(ctx, "agent_id", None)
            if not gid:
                return ("Not permitted: self-provisioning requires this run to act as a governed subject "
                        "(an API-key-scoped run). Operator/console runs must provision via the API.")
            eu = getattr(ctx, "end_user", None) or {}
            eu_id = str(eu.get("id") or "").strip() or None

            # Resolve the resource list: a single kind overrides; else the named template.
            if kind:
                resources = [{"kind": kind, "spec": {}}]
                template_id: str | None = None
            else:
                resources = templates.resources_for(template)
                if resources is None:
                    return f"Unknown template {template!r}. Choose one of: db, db+storage, db+storage+queue."
                template_id = template

            async with SessionLocal() as s:
                # Gate on the governed subject: default-deny capability + per-subject capacity cap.
                key = (await s.execute(
                    select(ApiKey).where(ApiKey.tenant_id == ctx.tenant_id, ApiKey.id == gid)
                )).scalar_one_or_none()
                if key is None:
                    return "Not permitted: this run's governed subject could not be resolved."
                if not ApiKeyService.allows(key, _PROVISION_CAP):
                    return f"Not permitted: this agent lacks the '{_PROVISION_CAP}' capability."
                try:
                    await ApiKeyService.enforce_capacity(s, key)
                except ProvisionNotAllowed as e:
                    return f"Cannot provision: {e}"

                try:
                    result = await bp.provision_resource_list(
                        s, ctx.tenant_id, ctx.project_id, agent_id=gid, end_user_id=eu_id,
                        resources=resources, template_id=template_id, name=(resource_name or None),
                    )
                except bp.ProvisionError as e:
                    return f"Provisioning failed: {e}"

            provisioned, errors = result["provisioned"], result["errors"]
            if not provisioned:
                return json.dumps({"provisioned": [], "errors": errors, "message": "Nothing was provisioned."})
            # Client-safe summary only — handles carry secret REFS (never values); omit them entirely.
            summary = [{"backend_id": h["backend_id"], "provider": h["provider"], "status": h["status"],
                        "endpoint_url": h.get("endpoint_url"), "template": h.get("template")} for h in provisioned]
            return json.dumps({"agent_id": gid, "end_user_id": eu_id, "provisioned": summary, "errors": errors})

        return StructuredTool.from_function(
            coroutine=provision_resource_tool, name=name,
            description=desc or ("Provision an isolated backend resource stack (database / storage / queue) for the "
                                 "current end user so their data is isolated from other users. Returns the resource handles."),
            args_schema=_ProvisionArgs,
        )

    if builtin == "claude_agent":
        return _build_claude_agent_tool(cfg, ctx)

    raise ValueError(f"Unknown builtin tool: {builtin!r}")


# --- Claude Agent SDK exposed as a callable tool -----------------------------------------------
# A ROS agent/deep_agent CALLS this tool to hand a self-contained, autonomous task to Anthropic's
# Claude Agent SDK (its native reasoning loop, file/shell/edit tools, MCP, subagents, and automatic
# skill invocation - none of which we reimplement, so none is compromised). Because the host ROS
# agent owns the ROS-side features (its own tools/toolsets/knowledge/components/middleware/budget/
# any-provider model + per-step tracing), this tool needs no ROS-tool bridge: it is the inner
# Claude loop, invoked on demand. The SDK is Anthropic-only and its loop is opaque to LangGraph,
# so ROS middleware does not wrap it and the model is a bare Claude id.
#
# LIVE STREAMING: as the SDK yields turns, we emit `custom` frames on the "claude_agent" channel
# via get_stream_writer() - the same mechanism pm_reason/emit_event use - so the workflow's live
# timeline shows per-turn activity (assistant text, tool_use, result) instead of one silent block.

class _ClaudeAgentArgs(BaseModel):
    task: str = Field(description="The self-contained task for the Claude agent to complete autonomously (it can read, edit, and run shell commands in its workspace).")
    workspace: str = Field(default="", description="Optional absolute working directory the agent operates in. Empty = the run's workspace, else a temp dir.")
    model: str = Field(default="", description="Optional bare Anthropic model id (e.g. 'claude-sonnet-4-5'). Empty = the CLI's default.")
    system_prompt: str = Field(default="", description="Optional extra system prompt appended to Claude's own agent prompt for this task.")


def _build_claude_agent_tool(cfg: dict, ctx):
    import os

    name = cfg.get("name", "claude_agent")
    desc = cfg.get("description", "")
    # Node-level defaults from the tool config; per-call args (task/workspace/model/system_prompt)
    # override where provided. permission_mode / max_turns / tool allow-lists are set here (not
    # exposed as call args) so the operator - not the calling model - governs them.
    default_permission = cfg.get("permission_mode", "acceptEdits")
    default_max_turns = int(cfg.get("max_turns") or 40)
    allowed_tools = cfg.get("allowed_tools")
    disallowed_tools = cfg.get("disallowed_tools")
    default_workspace = cfg.get("workspace") or ""
    default_model = cfg.get("model") or ""
    default_system = cfg.get("system_prompt") or ""
    # Native SDK MCP servers the calling operator granted this tool (agent-scoped MCP, pre-loaded
    # by the runtime assembler). Native SDK feature - no ROS-tool bridge.
    mcp_servers = cfg.get("mcp_servers")

    creds = getattr(ctx, "provider_credentials", None) or {}
    anthropic_key = creds.get("anthropic")

    def _resolve_cwd(workspace: str) -> str:
        """Per-call workspace arg > the tool's configured default > ros.util.workspace's resolution
        (ROS_CLAUDE_CODE_WORKSPACE > `<workspace_root>/<run_id>` > temp dir). Sharing the run's
        directory with the claude_code node is deliberate: a workflow that hands work between the
        two sees one set of files."""
        from ros.util.workspace import resolve_workspace

        return resolve_workspace(workspace or default_workspace, ctx, prefix="ros-claude-agent-")

    def _emit(event: str, payload: dict) -> None:
        """Push a live per-turn activity frame to the run stream (no-op with no active writer)."""
        try:
            from langgraph.config import get_stream_writer
            get_stream_writer()({"channel": "claude_agent", "payload": {"event": event, **payload}})
        except Exception:  # noqa: BLE001 - no active stream writer (ainvoke / non-SSE)
            pass

    async def claude_agent_tool(task: str, workspace: str = "", model: str = "", system_prompt: str = "") -> str:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as e:  # pragma: no cover - optional extra
            raise ImportError(
                "The claude_agent tool needs the Claude Agent SDK: install `.[claude_code]` "
                "(also requires the `claude` CLI + Node on PATH)."
            ) from e

        cwd = _resolve_cwd(workspace)
        opts: dict = {
            "cwd": cwd,
            "permission_mode": default_permission,
            "max_turns": default_max_turns,
        }
        eff_model = model or default_model
        if eff_model:
            opts["model"] = eff_model
        eff_system = system_prompt or default_system
        if eff_system:
            opts["system_prompt"] = eff_system
        if allowed_tools is not None:
            opts["allowed_tools"] = list(allowed_tools)
        if disallowed_tools is not None:
            opts["disallowed_tools"] = list(disallowed_tools)
        if mcp_servers:
            opts["mcp_servers"] = mcp_servers
        options = ClaudeAgentOptions(**opts)

        # The SDK's CLI subprocess reads ANTHROPIC_API_KEY from env; overlay the project's governed
        # key for the duration of this call only, then restore (no leak into the long-lived env).
        restore = None
        if anthropic_key:
            restore = os.environ.get("ANTHROPIC_API_KEY")
            os.environ["ANTHROPIC_API_KEY"] = anthropic_key

        text_chunks: list[str] = []
        cost_usd = None
        _emit("start", {"task": task[:200], "cwd": cwd, "model": eff_model or "(default)"})
        try:
            async for message in query(prompt=task, options=options):
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    for block in getattr(message, "content", []) or []:
                        bkind = type(block).__name__
                        if bkind == "TextBlock":
                            t = getattr(block, "text", "") or ""
                            if t:
                                text_chunks.append(t)
                                _emit("assistant", {"text": t[:2000]})
                        elif bkind == "ToolUseBlock":
                            # Live per-turn tool activity: what tool Claude is invoking, mid-loop.
                            _emit("tool_use", {"tool": getattr(block, "name", "?"),
                                               "input": _clip(getattr(block, "input", None))})
                elif kind == "ResultMessage":
                    result_text = getattr(message, "result", None)
                    if result_text:
                        text_chunks = [result_text]  # authoritative final answer
                    cost_usd = getattr(message, "total_cost_usd", None)
                    if getattr(message, "is_error", False):
                        _emit("error", {"detail": str(result_text)[:500]})
        finally:
            if anthropic_key:
                if restore is None:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                else:
                    os.environ["ANTHROPIC_API_KEY"] = restore

        final = "\n".join(c for c in text_chunks if c).strip() or "(claude_agent: empty result)"
        _emit("done", {"cost_usd": cost_usd, "cwd": cwd})
        return final

    return StructuredTool.from_function(
        coroutine=claude_agent_tool, name=name,
        description=desc or (
            "Delegate a self-contained, autonomous task to the Claude agent: it plans, reads/edits "
            "files, and runs shell commands in a workspace, then returns its final result. Use for "
            "multi-step coding, refactoring, file, or research tasks you can hand off wholesale."
        ),
        args_schema=_ClaudeAgentArgs,
    )


def _clip(value, n: int = 300):
    """Bound a tool-use input's size before it enters an activity frame (avoid bloat)."""
    try:
        import json as _json
        s = _json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(value)
    return s[:n]


class _KbQuery(BaseModel):
    query: str = Field(description="A focused search query - use one per distinct sub-question, in your own words (not the user's whole message).")


def build_knowledge_capability_tools(knowledge: dict | None, ctx) -> list:
    """Built-in knowledge access attached straight to an agent node via its `knowledge`
    config - no separate Tool row needed. Two independent, separately-toggleable tools:

    - RAG  (`search_knowledge_base`): vector search over knowledge DOCUMENTS, scoped to the
      configured folders (empty = all).
    - Q&A  (`lookup_faq`): semantic match over curated FAQ / Q&A pairs, scoped to the
      configured kinds (empty = all).

    Unlike the fixed `retrieval` node (one search per run, before the agent), these are
    agent-driven: the agent decides when to search and rewrites the query per
    sub-question - which is what lets ONE agent answer multi-part questions.
    """
    tools: list = []
    if not knowledge:
        return tools
    rag = knowledge.get("rag") or {}
    qa = knowledge.get("qa") or {}

    if rag.get("enabled"):
        from ros.knowledge.embeddings import DEFAULT_MIN_SCORE, DEFAULT_RERANK_MIN_SCORE
        from ros.knowledge.store import citation_for

        folders = rag.get("folders") or None
        top_k = int(rag.get("top_k") or 4)
        min_score = rag.get("min_score", DEFAULT_MIN_SCORE)
        rerank_min_score = rag.get("rerank_min_score", DEFAULT_RERANK_MIN_SCORE)
        hybrid = bool(rag.get("hybrid", False))
        rerank = bool(rag.get("rerank", False))
        rerank_top_n = rag.get("rerank_top_n")
        mmr = bool(rag.get("mmr", False))
        mmr_lambda = rag.get("mmr_lambda", 0.5)
        scope = f" (folders: {', '.join(folders)})" if folders else ""

        def _floor_ok(h) -> bool:
            # Threshold on the correct scale: rerank -> cross-encoder sigmoid (rerank_min_score);
            # hybrid -> real cosine in vector_score (Hit.score there is the fused rank); else cosine.
            if rerank:
                return rerank_min_score is None or h.score >= rerank_min_score
            cos = h.vector_score if hybrid else h.score
            return min_score is None or cos is None or cos >= min_score

        async def search_knowledge_base(query: str) -> str:
            from ros.db.base import SessionLocal
            from ros.services.knowledge import KnowledgeService

            async with SessionLocal() as s:
                try:
                    embedder = await KnowledgeService.embedder_for_project(s, ctx.tenant_id, ctx.project_id)
                    vec = await embedder.aembed_query(query)
                    hits = await KnowledgeService.search(
                        s, ctx.tenant_id, ctx.project_id, query, top_k=top_k,
                        folders=folders, embedder=embedder, embedding=vec, hybrid=hybrid,
                        rerank=rerank, rerank_top_n=rerank_top_n, mmr=mmr, mmr_lambda=mmr_lambda,
                    )
                except Exception:  # noqa: BLE001 - store empty / not ready
                    hits = []
                hits = [h for h in hits if _floor_ok(h)]
            blocks = [f"[Doc {i + 1}{f' · {c}' if (c := citation_for(h.metadata)) else ''} · score {h.score:.2f}] {h.text}"
                      for i, h in enumerate(hits)]
            return "\n\n".join(blocks) if blocks else "No relevant documents found in the knowledge base for this query."

        tools.append(StructuredTool.from_function(
            coroutine=search_knowledge_base, name="search_knowledge_base",
            description=f"Search the project knowledge-base DOCUMENTS{scope} for grounding facts. Call once per distinct sub-question with a focused query.",
            args_schema=_KbQuery,
        ))

    if qa.get("enabled"):
        kinds = qa.get("kinds") or None
        threshold = float(qa.get("threshold", 0.3))
        top_k_qa = int(qa.get("top_k") or 3)
        scope = f" (kinds: {', '.join(kinds)})" if kinds else ""

        async def lookup_faq(query: str) -> str:
            from ros.db.base import SessionLocal
            from ros.services.knowledge import KnowledgeService

            async with SessionLocal() as s:
                try:
                    embedder = await KnowledgeService.embedder_for_project(s, ctx.tenant_id, ctx.project_id)
                    vec = await embedder.aembed_query(query)
                    qa_hits = await KnowledgeService.top_qa(
                        s, ctx.tenant_id, ctx.project_id, query, top_k=top_k_qa,
                        threshold=threshold, kinds=kinds, embedder=embedder, embedding=vec,
                    )
                except Exception:  # noqa: BLE001
                    qa_hits = []
            blocks = [f"[FAQ] Q: {q['question']}\nA: {q['answer']}" for q in qa_hits]
            return "\n\n".join(blocks) if blocks else "No matching FAQ / Q&A entry found for this query."

        tools.append(StructuredTool.from_function(
            coroutine=lookup_faq, name="lookup_faq",
            description=f"Look up curated FAQ / Q&A answers{scope}. Prefer these exact, approved answers when one matches the user's question.",
            args_schema=_KbQuery,
        ))

    return tools
