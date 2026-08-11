# Running ROS on a Supabase-style stack

ROS's persistence is env-selected seams, so you can use **Supabase for the managed data layer** —
Postgres, pgvector, and object storage — while keeping compute (api + arq worker + Redis) on
Railway/Fly/Render and ROS's own auth. This is the pragmatic "use Supabase like the agent-infra
platforms" path: less infra to run, no code changes.

## What fits today (env-only, no code)

| Supabase primitive | ROS seam / env | Notes |
|---|---|---|
| **Postgres (app DB)** | `ROS_DATABASE_URL=postgresql+asyncpg://…` | Point at your Supabase connection string. RLS already assumed (`infra/postgres_rls.sql`) — ROS sets `app.current_tenant` itself, independent of Supabase Auth. |
| **Postgres (LangGraph checkpointer)** | `ROS_CHECKPOINT_BACKEND=postgres` (+ optional `ROS_CHECKPOINT_POSTGRES_URL`, else falls back to `ROS_DATABASE_URL`) | ⚠️ **Pooler caveat below.** |
| **pgvector** | `ROS_VECTOR_BACKEND=pgvector` | Supabase ships the `vector` extension — removes the local chroma volume and works across replicas. Run `CREATE EXTENSION vector;` once. |
| **Storage (S3-compatible)** | `ROS_ARTIFACT_STORE=s3`, `ROS_S3_ENDPOINT_URL=https://<ref>.storage.supabase.co/storage/v1/s3`, `ROS_S3_REGION`, `ROS_S3_ACCESS_KEY_ID`, `ROS_S3_SECRET_ACCESS_KEY`, `ROS_S3_ADDRESSING_STYLE=path` | Supabase Storage exposes an S3 endpoint + presigned URLs — exactly what the S3 backend targets. Path-style addressing is required. |

### ⚠️ Pooler / prepared-statement caveat (important)
LangGraph's `AsyncPostgresSaver` and asyncpg use **prepared statements**, which break under
Supabase's default **transaction-mode** pooler (pgbouncer, port `6543`). Use one of:
- the **session-mode** pooler, or
- the **direct** connection (port `5432`), or
- append `?prepared_statement_cache_size=0` (asyncpg) where applicable.

Mind Supabase's connection limit — the checkpointer opens its own libpq connection in addition to
the SQLAlchemy pool, and its `.setup()` creates its own tables on first boot.

## What does NOT fit (needs work or stays as-is)

- **Auth** — ROS ships a self-contained **HS256 JWT** identity system (`ros/security.py`, users/
  roles/tenants in `ros/services/auth.py`). There is no OIDC/SSO. Using **Supabase Auth (GoTrue)**
  would be net-new code: verify Supabase's RS256 JWTs (JWKS), map `auth.uid()`/claims → ROS
  `User`/`Tenant`/role, and reconcile the `app.current_tenant` GUC with Supabase RLS. Filed as an
  optional follow-up (`ROS_AUTH_BACKEND` seam) — not required to run on Supabase.
- **Realtime** — ROS streams via **SSE** and coordinates via **Redis** (rate limits, idempotency,
  token revocation, arq queue, singleton leases). Supabase Realtime doesn't replace Redis; **Redis
  is still required** for any multi-replica deploy.
- **Edge Functions** — the runtime is a long-lived FastAPI/uvicorn app + an arq worker with a
  durable Postgres checkpointer and background loops (reaper/retention/scheduler). This is stateful
  and long-running; it does **not** map onto stateless edge functions. Edge functions could call
  the API, not host it.
- **Secrets master key** — still needs `ROS_SECRET_KEY` (from a KMS/Vault) or a persistent volume
  for the Fernet key; Supabase provides no equivalent.

## Minimal env for "Supabase data layer, compute elsewhere"

```bash
# --- Supabase Postgres (session-mode pooler or direct 5432; NOT transaction-mode 6543) ---
ROS_DATABASE_URL=postgresql+asyncpg://postgres.<ref>:<pw>@<host>:5432/postgres
ROS_CHECKPOINT_BACKEND=postgres
ROS_VECTOR_BACKEND=pgvector           # run: CREATE EXTENSION vector;

# --- Supabase Storage (S3-compatible) ---
ROS_ARTIFACT_STORE=s3
ROS_S3_ENDPOINT_URL=https://<ref>.storage.supabase.co/storage/v1/s3
ROS_S3_REGION=us-east-1
ROS_S3_ACCESS_KEY_ID=<storage-access-key>
ROS_S3_SECRET_ACCESS_KEY=<storage-secret>
ROS_S3_ADDRESSING_STYLE=path
ROS_ARTIFACT_BUCKET=ros-artifacts     # create the bucket in Supabase first

# --- still ROS-owned (not Supabase) ---
ROS_REDIS_URL=redis://…               # required for multi-replica
ROS_SECRET_KEY=<32-byte urlsafe b64>  # or mount a persistent volume
ROS_JWT_SECRET=<random>
```

## Net
**Postgres + pgvector + Storage drop in via env today**; **Auth, Realtime, and Edge Functions do
not** — keep ROS auth + Redis + a long-lived container. The only real gotcha is the pooler mode for
the LangGraph checkpointer.
