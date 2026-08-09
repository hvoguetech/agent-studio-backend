"""FastAPI application factory + lifespan.

Builds our own server on the MIT LangChain/LangGraph framework - never depends on
`langgraph-api` or LangSmith. Lifespan initializes the DB, the durable-execution
checkpointer, and dev seed data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import ros
from ros.config import settings
from ros.db import SessionLocal, init_db
from ros.db.seed import bootstrap, seed_demo_data
from ros.routers import (
    agents,
    assistant,
    audit,
    auth,
    auth_providers,
    channels,
    components,
    connections,
    conversations,
    embed,
    embed_public,
    evals,
    handoff,
    health,
    hooks,
    knowledge,
    mcp_clients,
    mcp_oauth,
    mcp_server,
    mcp_tokens,
    models,
    nodes,
    oauth,
    pricing,
    project_run,
    projects,
    runs,
    secrets,
    stats,
    tool_sets,
    tools,
    traces,
    versions,
    workflows,
)
from ros.routers import (
    triggers as triggers_router,
)
from ros.util.http import aclose_shared_client
from ros.util.logging_setup import configure_logging
from ros.util.metrics import RequestMetricsMiddleware


async def _make_checkpointer(stack: AsyncExitStack):
    """Durable-execution checkpointer. Selected by ROS_CHECKPOINT_BACKEND:
    - "postgres": durable + shared across workers (REQUIRED for prod/HITL; audit P2).
    - "memory": ephemeral (tests / throwaway).
    - "sqlite" (default): local file; fine for single-worker dev, lost on restart."""
    backend = (settings.checkpoint_backend or "sqlite").lower()
    if backend == "memory" or settings.checkpoint_db == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as e:  # pragma: no cover - optional extra
            raise RuntimeError(
                "ROS_CHECKPOINT_BACKEND=postgres needs langgraph-checkpoint-postgres "
                "(pip install -e '.[postgres]')."
            ) from e
        dsn = settings.checkpoint_postgres_url or settings.database_url
        # LangGraph wants a plain libpq DSN, not the SQLAlchemy +asyncpg/+psycopg form.
        for prefix in ("+asyncpg", "+psycopg", "+psycopg2"):
            dsn = dsn.replace(prefix, "")
        cp = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
        try:
            await cp.setup()
        except Exception:  # noqa: BLE001 - setup is idempotent
            pass
        return cp
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    cp = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(settings.checkpoint_db))
    try:
        await cp.setup()
    except Exception:  # noqa: BLE001 - setup is idempotent; ignore "already exists"
        pass
    return cp


async def _reaper_loop(app: FastAPI) -> None:
    """Periodically reap runs stuck in queued/running (never streamed, or driver died) so
    they can't linger forever (audit F3). Routed through the execution backend (A/C12); gated
    per-tick on the singleton lease so a multi-replica deployment reaps once."""
    log = logging.getLogger("ros.reaper")
    backend = app.state.execution_backend
    while True:
        try:
            await asyncio.sleep(300)
            async with backend.singleton("reaper", ttl_seconds=600) as is_leader:
                if is_leader:
                    await backend.reclaim_orphans()
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 - keep the reaper alive across failures
            log.exception("reaper tick failed")


async def _retention_loop(app: FastAPI) -> None:
    """Purge traces/spans/runs past each project's retention horizon and audit logs past the
    workspace horizon, on a timer (finding e). Singleton-gated so a multi-replica deployment
    purges once. No-op unless a retention window is configured."""
    log = logging.getLogger("ros.retention")
    backend = app.state.execution_backend
    interval = max(60, int(settings.retention_interval_seconds or 3600))
    while True:
        try:
            await asyncio.sleep(interval)
            async with backend.singleton("retention", ttl_seconds=interval) as is_leader:
                if is_leader:
                    from ros.services.retention import RetentionService

                    await RetentionService.purge_expired(
                        checkpointer=getattr(app.state, "checkpointer", None)
                    )
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 - keep the retention loop alive across failures
            log.exception("retention tick failed")


async def _scheduler_loop(app: FastAPI) -> None:
    """Fire due `schedule` / `app_event` triggers once a minute via the execution backend
    (A/C12). Singleton-gated so exactly one instance fires; a cron-driven backend no-ops the
    tick and schedules itself instead."""
    log = logging.getLogger("ros.scheduler")
    backend = app.state.execution_backend
    while True:
        try:
            await asyncio.sleep(60)
            async with backend.singleton("scheduler", ttl_seconds=120) as is_leader:
                if is_leader:
                    await backend.run_scheduler_tick()
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 - keep the scheduler alive across failures
            log.exception("scheduler tick failed")


def _preload_heavy_modules() -> None:
    """Import the slow modules off the critical path. First import of langchain_openai
    / chromadb costs ~22s / ~9s on this machine (AV scanning); doing it in a daemon
    thread at startup means the first real run doesn't pay it."""
    import importlib

    for mod in ("langchain_openai", "chromadb", "langchain.chat_models"):
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001 - optional providers may be missing
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Install the IPv4-first DNS resolver before anything opens a connection, so outbound calls
    # (LLM providers, REST tools, DB, redis) never pay the multi-second AAAA-lookup stall.
    if settings.prefer_ipv4_egress:
        from ros.util.netfix import install_prefer_ipv4_dns

        install_prefer_ipv4_dns()
    settings.ensure_dirs()
    problems = settings.validate_production()
    if problems:
        # Refuse to serve a misconfigured production install (default secrets, auth off,
        # SSRF guard off, SQLite, non-durable checkpointer, unsandboxed code). Set the
        # flagged env vars before deploying. Enforced for every non-dev environment (S6).
        raise RuntimeError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))
    for warn in settings.startup_warnings():
        logging.getLogger("ros.config").warning("INSECURE CONFIG: %s", warn)
    await init_db()
    threading.Thread(target=_preload_heavy_modules, name="ros-preload", daemon=True).start()
    app.state.exit_stack = AsyncExitStack()
    app.state.checkpointer = await _make_checkpointer(app.state.exit_stack)
    app.state.store = None
    # Resolve the execution backend (A/C12): "local" (MIT, default) or a plugin. The core
    # calls it for offloaded execution, scheduling, reclaim, and singleton coordination.
    from ros.execution import get_backend

    app.state.execution_backend = get_backend()
    await app.state.execution_backend.startup(app)
    async with SessionLocal() as session:
        tenant_id = await bootstrap(session)
        if settings.seed_demo:
            await seed_demo_data(session, tenant_id)
        app.state.tenant_id = tenant_id
        from ros.routers.pricing import load_pricing_overrides

        await load_pricing_overrides(session)
    if settings.otel_enabled:
        from ros.tracing import otel

        otel.configure()
    bg_tasks: list[asyncio.Task] = []
    # Each sweep gates per-tick on `backend.singleton(...)` so it runs on exactly one instance
    # (Redis lease when present, else the `scheduler_leader` flag - same behavior as before).
    if settings.enable_scheduler:
        bg_tasks.append(asyncio.create_task(_scheduler_loop(app), name="ros-scheduler"))
    bg_tasks.append(asyncio.create_task(_reaper_loop(app), name="ros-reaper"))
    if settings.enable_retention:
        bg_tasks.append(asyncio.create_task(_retention_loop(app), name="ros-retention"))
    yield
    for t in bg_tasks:
        t.cancel()
    for t in bg_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t
    from ros.util.tasks import drain

    await drain()
    with contextlib.suppress(Exception):
        from ros.tools.mcp import close_all

        await close_all()
    with contextlib.suppress(Exception):
        from ros.queue import close_pool

        await close_pool()
    await aclose_shared_client()
    await app.state.exit_stack.aclose()


def create_app() -> FastAPI:
    configure_logging()  # A/C5: install JSON logging when ROS_LOG_JSON is set (no-op otherwise)
    from ros.authz import audit_route_coverage, default_deny_guard

    app = FastAPI(
        title="ROS API",
        version=ros.__version__,
        description="Self-hosted platform for building, testing, and shipping LangChain/LangGraph agents.",
        lifespan=lifespan,
        # Default-deny backstop (B/E4): every route must declare a permission or be marked
        # public; an undeclared route fails closed. Enforced structurally, per request.
        dependencies=[Depends(default_deny_guard)],
    )
    # Host-header allow-list (defense-in-depth against Host-header attacks). Empty => any (dev).
    if settings.trusted_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Coarse per-IP request ceiling (api_rate_limit_per_minute) as a blunt DoS guard;
    # complements the per-surface limits. Health/SSE exempt (finding a).
    if settings.enable_global_rate_limit:
        from ros.util.ratelimit import GlobalRateLimitMiddleware

        app.add_middleware(GlobalRateLimitMiddleware)
    # Audit all successful mutations (pure ASGI; safe with SSE streams).
    from ros.audit_middleware import AuditMiddleware

    app.add_middleware(AuditMiddleware)
    # A/C5: RED-ish request/response counters. Added LAST so it's the OUTERMOST middleware and
    # counts every request - including ones short-circuited by the rate limiter (429) above.
    app.add_middleware(RequestMetricsMiddleware)
    for r in (
        health.router, auth.router, auth.team_router, auth.workspace_router, auth.apikeys_router,
        audit.router, oauth.router, hooks.router,
        models.router, nodes.router, projects.router, workflows.router, runs.router, project_run.router,
        tool_sets.router, tools.router, components.router, embed.router, embed_public.router, auth_providers.router, connections.router, secrets.router, agents.router,
        knowledge.router, knowledge.qa_router, traces.router, conversations.router, assistant.router, stats.router,
        triggers_router.router, channels.router, channels.public, handoff.router, evals.router,
        pricing.router, mcp_oauth.router, mcp_server.router, mcp_tokens.router, mcp_clients.router, versions.router,
    ):
        app.include_router(r)
    # Authorization coverage audit (B/E4): log loudly if any route lacks a permission /
    # public declaration. These still fail closed at request time via default_deny_guard;
    # the CI coverage test (tests/test_authz.py) turns this into a hard failure.
    undeclared = audit_route_coverage(app)
    if undeclared:
        log = logging.getLogger("ros.authz")
        for methods, path, name in undeclared:
            log.error(
                "ROUTE MISSING AUTHZ DECLARATION (fails closed): %s %s [%s]",
                ",".join(methods) or "?",
                path,
                name,
            )
    return app


app = create_app()
