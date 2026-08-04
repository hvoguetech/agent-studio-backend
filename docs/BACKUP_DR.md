# Backup, Disaster Recovery & DB connection pooling (A/C6)

Guidance for running Forge's Postgres durably in production. Forge stores all durable state in
Postgres (application data + the LangGraph checkpointer when `FORGE_CHECKPOINT_BACKEND=postgres`);
object storage (uploads/knowledge, A/C7) is separate.

## Connection pooling (shipped)

Each API/worker replica keeps a tuned SQLAlchemy `QueuePool` against Postgres (SQLite dev/test uses
`NullPool`). Configure per replica:

| Setting | Default | Notes |
|---|---|---|
| `FORGE_DB_POOL_SIZE` | 10 | Steady-state connections held open. |
| `FORGE_DB_MAX_OVERFLOW` | 20 | Extra burst connections above `pool_size`. |
| `FORGE_DB_POOL_TIMEOUT` | 30 | Seconds to wait for a connection before erroring. |
| `FORGE_DB_POOL_RECYCLE` | 1800 | Recycle a connection after this many seconds (drops stale ones). |
| `FORGE_DB_POOL_PRE_PING` | true | Validate a pooled connection before use (survives a DB failover/restart). |

**Sizing:** peak connections per replica ≈ `pool_size + max_overflow`. Keep
`replicas × (pool_size + max_overflow)` comfortably **below Postgres `max_connections`**, leaving
headroom for admin/maintenance connections.

**PgBouncer:** for very high replica counts put PgBouncer in front. Use **session** pooling mode —
**transaction** pooling breaks (a) the A/C2 leader-election session advisory locks and (b) any
session-scoped state (`SET LOCAL` RLS GUC is per-transaction and is fine; prepared statements are
not). If you must use transaction pooling, run the scheduler/reaper leader election on Redis
(`FORGE_REDIS_URL`) instead of the Postgres advisory lock.

## Backups & Point-in-Time Recovery

Prefer **managed Postgres** (RDS / Cloud SQL / Neon) with automated daily snapshots + WAL archiving
so you get PITR and one-click restore. Self-managed baseline:

- **Logical** (portable, per-database): scheduled `pg_dump -Fc` → object storage, retained N days.
- **Physical + PITR** (low RPO): `pg_basebackup` + continuous WAL archiving (`archive_command` to
  object storage, or `pgBackRest`/`wal-g`). Target RPO ≤ 5 min, RTO ≤ 1 h for a hosted plan.

**Restore drill (do it regularly):** restore the latest backup into a scratch instance, run
`alembic upgrade head`, boot the API against it, and confirm a smoke run completes. An untested
backup is not a backup.

## Replication / HA

- A **synchronous or async standby** (streaming replication) for failover; managed offerings do
  this for you. `pool_pre_ping` above lets replicas recover automatically after a failover promotes
  the standby (stale connections are discarded and reopened to the new primary).
- Optional **read replicas** for heavy read workloads (Traces/analytics); route reads explicitly —
  Forge does not split reads/writes automatically today.

## Follow-ups (infra/ops, out of scope for the app)

- Automated PITR + retention policy and a scripted, scheduled **restore-drill** in CI/cron.
- IaC for managed Postgres + PgBouncer (ties into A/C4 Helm/k8s).
- Documented RPO/RTO targets per plan tier.
