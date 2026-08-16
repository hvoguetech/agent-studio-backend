"""Resource provisioning orchestrator — persistence, rollback, and governance around ResourceProvider.

`provision_resource()` asks the selected provider (Supabase today; Railway/Freestyle/Queue next) to
create an ISOLATED resource, stores its creds as project-scoped `secret://` refs, and records a
`ProvisionedBackend` row scoped per-agent. It is GOVERNED BY THE CALLER — the tool gate / route
enforce entitlement + authz + budget admission BEFORE this runs; this is not itself an authz
boundary. On persistence failure it rolls back (delete written secrets + provider.teardown), so a
partial provision never leaks a resource or credentials.

`runtime_env()` gathers all of an agent's active resources into the env its run gets at runtime —
the "access at runtime" half of the requirement. `provision_backend()` / `teardown_backend()` are
the Supabase-era names kept as thin wrappers.
"""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ros.models import ProvisionedBackend
from ros.services.providers.base import (
    ENDPOINT_ENV_VARS,
    SECRET_ENV_VARS,
    ProvisionError,
    get_provider,
)
from ros.services.secrets import SecretService

logger = logging.getLogger(__name__)

__all__ = [
    "ProvisionError", "is_enabled", "provision_resource", "provision_backend",
    "teardown_resource", "teardown_backend", "runtime_env", "resolved_runtime_env",
    "runtime_env_for_run",
]


def is_enabled(kind: str = "supabase") -> bool:
    """Whether the given provider kind is configured. Defaults to supabase (back-compat)."""
    try:
        return get_provider(kind).is_enabled()
    except ProvisionError:
        return False


def _secret_name(kind: str, logical: str, agent_id: str | None, end_user_id: str | None = None) -> str:
    """Per-(agent, end-user) secret name so resources are isolated and don't collide
    (e.g. `supabase_database_url__<agent_id>__u_<hash>`); project-scoped when there's no agent. The
    end-user id is hashed so an arbitrary id (email/uuid) always yields a valid, bounded name."""
    name = f"{kind}_{logical}"
    if agent_id:
        name += f"__{agent_id}"
    if end_user_id:
        name += f"__u_{hashlib.sha1(end_user_id.encode()).hexdigest()[:10]}"
    return name


async def provision_resource(
    session: AsyncSession,
    tenant_id: str,
    project_id: str,
    *,
    agent_id: str | None = None,
    end_user_id: str | None = None,
    kind: str = "supabase",
    spec: dict | None = None,
    name: str | None = None,
) -> dict:
    """Provision an isolated resource of `kind` for an agent (optionally scoped to one END USER via
    `end_user_id`, the forUser model) and store its creds as secret refs.

    Returns a handle: {backend_id, provider, project_ref, status, endpoint_url, secret_refs, config,
    + provider client-safe extras}. Secret values (service keys, DB URLs) live only as `secret://`
    refs, never in the handle."""
    spec = spec or {}
    kind = (kind or spec.get("provider") or "railway-postgres").lower()
    provider = get_provider(kind)  # raises ProvisionError for an unknown kind
    if not provider.is_enabled():
        raise ProvisionError(f"{kind} provisioning not configured")
    # Per-project managed-backend cap (no-op unless project.config.budgets.max_backends is set).
    from ros.services.budget import enforce_provision_admission
    await enforce_provision_admission(session, tenant_id, project_id)

    display = name or (f"agent-{agent_id[:8]}" if agent_id else f"proj-{project_id[:8]}")
    outcome = await provider.provision(name=display, spec=spec)

    written: list[str] = []
    refs: dict[str, str] = {}
    try:
        for logical, (value, skind) in outcome.secrets.items():
            if not value:
                continue
            nm = _secret_name(kind, logical, agent_id, end_user_id)
            await SecretService.write(session, tenant_id, project_id, name=nm, value=value, kind=skind)
            written.append(nm)
            refs[logical] = f"secret://proj/{nm}"

        row = ProvisionedBackend(
            tenant_id=tenant_id, project_id=project_id, agent_id=agent_id, end_user_id=end_user_id,
            provider=kind, project_ref=outcome.external_id, status="active", region=spec.get("region"),
            endpoint_url=outcome.endpoint_url, secret_refs=refs, config=outcome.config or {},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    except Exception as e:
        for nm in written:
            try:
                await SecretService.delete(session, tenant_id, project_id, name=nm)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.warning("provision rollback: failed to remove secret %s", nm)
        try:
            await provider.teardown(external_id=outcome.external_id, config=outcome.config)
        except Exception:  # noqa: BLE001
            logger.warning("provision rollback: teardown failed for %s", outcome.external_id)
        raise ProvisionError(f"failed to persist provisioned {kind} resource ({outcome.external_id}): {e}") from e

    return {
        "backend_id": row.id,
        "provider": kind,
        "project_ref": outcome.external_id,
        "status": row.status,
        "endpoint_url": outcome.endpoint_url,
        "secret_refs": refs,
        "config": outcome.config or {},
        **outcome.public,  # client-safe extras (e.g. anon_key)
    }


async def provision_backend(
    session: AsyncSession, tenant_id: str, project_id: str,
    *, agent_id: str | None = None, end_user_id: str | None = None,
    spec: dict | None = None, name: str | None = None,
) -> dict:
    """Convenience wrapper: kind is taken from spec.provider, default 'railway-postgres'."""
    spec = spec or {}
    return await provision_resource(
        session, tenant_id, project_id, agent_id=agent_id, end_user_id=end_user_id,
        kind=(spec.get("provider") or "railway-postgres"), spec=spec, name=name,
    )


async def teardown_resource(
    session: AsyncSession, tenant_id: str, project_id: str, *, backend_id: str
) -> dict:
    """Tear down a provisioned resource: provider teardown + delete its secret refs + the row."""
    row = (await session.execute(
        select(ProvisionedBackend).where(
            ProvisionedBackend.id == backend_id,
            ProvisionedBackend.tenant_id == tenant_id,
            ProvisionedBackend.project_id == project_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise ProvisionError(f"provisioned resource {backend_id} not found")

    if row.project_ref:
        try:
            await get_provider(row.provider).teardown(external_id=row.project_ref, config=row.config)
        except ProvisionError:
            logger.warning("teardown: no provider for kind %s (resource %s)", row.provider, backend_id)
    for ref in (row.secret_refs or {}).values():
        nm = ref.rsplit("/", 1)[-1]
        try:
            await SecretService.delete(session, tenant_id, project_id, name=nm)
        except Exception:  # noqa: BLE001
            logger.warning("teardown: failed to delete secret %s", nm)

    project_ref = row.project_ref
    await session.delete(row)
    await session.commit()
    return {"backend_id": backend_id, "status": "deleted", "project_ref": project_ref}


# Back-compat alias (the Supabase-era name).
teardown_backend = teardown_resource


async def runtime_env(
    session: AsyncSession, tenant_id: str, project_id: str, *, agent_id: str,
    end_user_id: str | None = None,
) -> dict[str, str]:
    """The environment an agent's provisioned resources expose AT RUNTIME: standard env var name ->
    a `secret://` ref (creds) or endpoint URL. This is how a running agent (its tools/code, and any
    Railway service / Freestyle VM it owns) reaches the DB / queue / sandbox / service it provisioned.

    Isolated per agent (agent_id-scoped). With `end_user_id` set (the forUser model), returns the
    agent-SHARED resources (end_user_id NULL) UNION this end user's private ones, with the end user's
    overriding the shared on an env-var collision — so an agent serving many users gets each user's
    own isolated data while still sharing the agent-level infra. Without it, only the shared set."""
    if end_user_id is None:
        eu_cond = ProvisionedBackend.end_user_id.is_(None)
    else:
        eu_cond = or_(
            ProvisionedBackend.end_user_id.is_(None),
            ProvisionedBackend.end_user_id == end_user_id,
        )
    rows = (await session.execute(
        select(ProvisionedBackend).where(
            ProvisionedBackend.tenant_id == tenant_id,
            ProvisionedBackend.project_id == project_id,
            ProvisionedBackend.agent_id == agent_id,
            ProvisionedBackend.status == "active",
            eu_cond,
        )
    )).scalars().all()
    # Shared (end_user_id NULL) first so this end user's resources override on a collision.
    rows.sort(key=lambda r: r.end_user_id is not None)
    env: dict[str, str] = {}
    for r in rows:
        for logical, ref in (r.secret_refs or {}).items():
            var = SECRET_ENV_VARS.get((r.provider, logical))
            if var:
                env[var] = ref
        if r.endpoint_url:
            var = ENDPOINT_ENV_VARS.get(r.provider)
            if var:
                env[var] = r.endpoint_url
    return env


async def resolved_runtime_env(
    session: AsyncSession, tenant_id: str, project_id: str, *, agent_id: str,
    end_user_id: str | None = None,
) -> dict[str, str]:
    """`runtime_env` with `secret://` refs RESOLVED to their values (endpoint URLs pass through) —
    the actual env injected into an agent's compute (e.g. a Freestyle VM) so the ros runtime inside
    it can connect to its durable resources. A ref that can't be read is skipped, not fatal."""
    refs = await runtime_env(session, tenant_id, project_id, agent_id=agent_id, end_user_id=end_user_id)
    out: dict[str, str] = {}
    for var, val in refs.items():
        if isinstance(val, str) and val.startswith(("secret://", "vault://")):
            try:
                resolved = await SecretService.store.read_ref(
                    tenant_id=tenant_id, project_id=project_id, ref=val
                )
            except Exception:  # noqa: BLE001 - a missing/unreadable secret is skipped, not fatal
                logger.warning("runtime env: could not resolve %s (%s)", var, val)
                continue
            out[var] = resolved if isinstance(resolved, str) else json.dumps(resolved)
        else:
            out[var] = val
    return out


async def runtime_env_for_run(session: AsyncSession, *, run_id: str, tenant_id: str) -> dict[str, str]:
    """The resolved provisioned-resource env for a RUN: its governed subject (Run.agent_id) scoped to
    the run's bound end_user (agent-shared UNION that end_user's private set). Empty when the run has
    no governed subject (operator/console/JWT run) — those inject nothing.

    Convenience wrapper the VM runtime entrypoint uses to export the agent's env to os.environ before
    driving, given only a run id; mirrors what build_compile_context injects on the DB path (2b)."""
    from ros.models import Run, Thread

    run = (await session.execute(
        select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if run is None or not run.agent_id:
        return {}
    thread = (await session.execute(
        select(Thread).where(Thread.id == run.thread_id, Thread.tenant_id == tenant_id)
    )).scalar_one_or_none()
    eu_id = str((((thread.meta if thread else None) or {}).get("end_user") or {}).get("id") or "") or None
    return await resolved_runtime_env(
        session, tenant_id, run.project_id, agent_id=run.agent_id, end_user_id=eu_id,
    )
