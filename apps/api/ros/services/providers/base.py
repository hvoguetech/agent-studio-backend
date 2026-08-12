"""ResourceProvider seam — the pluggable contract every provisionable resource implements.

Generalizes the (originally Supabase-only) provisioning path into a uniform seam so an agent can
provision an ISOLATED resource of any kind — Supabase project, Railway service, Freestyle VM,
BullMQ/Redis queue — the same way. A provider owns the EXTERNAL work (create / configure / teardown)
and returns a ProvisionOutcome; the orchestrator (services/backend_provisioning) owns persistence
(secret refs + the ProvisionedBackend row), rollback, and governance. Isolation is per-agent: each
agent gets its own instance of each resource, grouped as one environment (all rows sharing agent_id).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class ProvisionError(RuntimeError):
    """Resource provisioning failed. A provider cleans up its own partial external state before
    raising; the orchestrator additionally rolls back any secrets/rows it had written."""


@dataclass
class ProvisionOutcome:
    """What a provider returns after creating an isolated resource.

    external_id  — the provider's resource id (used for teardown).
    endpoint_url — the primary URL/host of the resource, if any.
    secrets      — logical name -> (value, kind); the orchestrator stores each as a secret:// ref.
    public       — client-safe fields to hand back to the agent (never secrets).
    config       — a report of any extra config applied + best-effort errors.
    """

    external_id: str
    endpoint_url: str | None = None
    secrets: dict[str, tuple[str, str]] = field(default_factory=dict)
    public: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)


@runtime_checkable
class ResourceProvider(Protocol):
    kind: str

    def is_enabled(self) -> bool:
        """True when the provider is configured (its creds/tokens are present)."""
        ...

    async def provision(self, *, name: str, spec: dict) -> ProvisionOutcome:
        """Create an isolated resource. On failure, clean up partial external state and raise
        ProvisionError (leave nothing leaked)."""
        ...

    async def teardown(self, *, external_id: str, config: dict | None = None) -> None:
        """Destroy the resource (best-effort, idempotent)."""
        ...


def get_provider(kind: str) -> ResourceProvider:
    """Resolve a provider by kind (lazy import so the core never eagerly loads every provider)."""
    k = (kind or "supabase").lower()
    if k == "supabase":
        from ros.services.providers.supabase import SupabaseProvider

        return SupabaseProvider()
    if k == "railway":
        from ros.services.providers.railway import RailwayProvider

        return RailwayProvider()
    if k in ("railway-postgres", "railway_postgres", "postgres"):
        from ros.services.providers.railway_postgres import RailwayPostgresProvider

        return RailwayPostgresProvider()
    if k in ("railway-storage", "railway_storage", "storage"):
        from ros.services.providers.railway_storage import RailwayStorageProvider

        return RailwayStorageProvider()
    if k == "queue":
        from ros.services.providers.queue import QueueProvider

        return QueueProvider()
    # NOTE: a run-level `freestyle` (or `e2b`) sandbox provider lands on the ros.execution_backends
    # seam, not here — see the standalone-runtime port.
    raise ProvisionError(f"unsupported backend provider: {kind!r}")


# How each resource's stored creds + endpoint surface as env vars to a running agent (runtime
# access). Keyed by (provider_kind, logical_secret_name) for secret refs, and by provider_kind for
# the endpoint URL. Providers add their entries here as they land.
SECRET_ENV_VARS: dict[tuple[str, str], str] = {
    ("supabase", "database_url"): "DATABASE_URL",
    ("supabase", "service_role_key"): "SUPABASE_SERVICE_ROLE_KEY",
    ("supabase", "anon_key"): "SUPABASE_ANON_KEY",
    ("railway-postgres", "database_url"): "DATABASE_URL",  # Railway-only DB
    ("railway-storage", "s3_access_key_id"): "S3_ACCESS_KEY_ID",
    ("railway-storage", "s3_secret_access_key"): "S3_SECRET_ACCESS_KEY",
    ("queue", "redis_url"): "REDIS_URL",  # a connection URL with creds -> stored as a secret ref
}
ENDPOINT_ENV_VARS: dict[str, str] = {
    "supabase": "SUPABASE_URL",
    "railway": "SERVICE_URL",
    "railway-storage": "S3_ENDPOINT",
}
