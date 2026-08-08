# Deploy the full stack to a new Railway project

End-to-end runbook to stand up the entire ROS stack — **api** (FastAPI/LangGraph),
**web** (Next.js console), **Postgres**, **Redis** — in a fresh Railway project.

> **Env prefix is `ROS_`.** The backend reads settings via pydantic-settings with
> `env_prefix="ROS_"` (see `apps/api/ros/config.py`). A var named `FORGE_*` (the pre-rebrand
> prefix) is **silently ignored** — the app falls back to insecure dev defaults (SQLite, default
> JWT/admin secrets) and the security guard is skipped. If you ever see the api healthy but on
> SQLite, this is why. Every backend var below MUST start with `ROS_`.

---

## 0. Architecture / what you're creating

```
                 ┌─────────── Railway project ───────────┐
 browser ─HTTPS→ │  web (Next.js :3000)                   │
                 │    │  /api/ros/* rewrite (server-side)  │
                 │    ▼  http://api.railway.internal:8000  │   ← private network (IPv6)
                 │  api (FastAPI/uvicorn :8000)            │
                 │    ├── Postgres   (data + LangGraph checkpointer)
                 │    ├── Redis      (rate limits / idempotency / token revocation)
                 │    └── Volume /app/.data  (Fernet master.key [+ chroma vectors])
                 └────────────────────────────────────────┘
```

- **api** — Dockerfile build (`apps/api/Dockerfile`); start command runs `alembic upgrade head`
  then `uvicorn ros.main:app`. Healthcheck `GET /readyz`. Needs a **volume at `/app/.data`**.
- **web** — Dockerfile build (`apps/web/Dockerfile`, Next.js standalone, listens on `:3000`).
  The browser talks to the api through a **same-origin proxy** `/api/ros/*` whose destination is
  **baked at build time** from the `ROS_API_URL` build arg — so `ROS_API_URL` must be set on the
  web service *before* it builds.
- **Postgres / Redis** — Railway managed plugins.

Repos: **`agent-studio-backend`** (this repo, public) and **`agent-studio-frontend`** (private).
Both keep the `apps/` layout and a root `railway.json` (Dockerfile builder).

---

## 1. Prerequisites

```bash
railway --version         # Railway CLI installed + logged in (railway login)
gh auth status            # to clone the private frontend
git clone git@github.com:marutsinghhvogue/agent-studio-backend.git
git clone git@github.com:marutsinghhvogue/agent-studio-frontend.git
```

Generate the secrets you'll need now (keep them somewhere safe):

```bash
python3 -c "import secrets; print('ROS_JWT_SECRET      =', secrets.token_urlsafe(48))"
python3 -c "import secrets; print('ROS_SERVICE_API_TOKEN=', secrets.token_urlsafe(36))"  # optional; >=24 chars
python3 -c "import secrets; print('ROS_BOOTSTRAP_ADMIN_PASSWORD=', secrets.token_urlsafe(12))"
# Fernet master key (only needed for multi-replica; single instance can use the volume file):
python3 -c "from cryptography.fernet import Fernet; print('ROS_SECRET_KEY     =', Fernet.generate_key().decode())"
```

---

## 2. Create the project + datastores

```bash
railway init --name ros                 # creates the project; note the project id
railway add --database postgres
railway add --database redis
```

Grab the ids/context you'll reference later:

```bash
railway status --json                   # project id, environment (usually "production")
railway service list --json             # Postgres / Redis service names
```

**pgvector (optional, for multi-replica vector search).** The default `ROS_VECTOR_BACKEND=chroma`
stores vectors on the api volume (single-writer — fine for one api replica). To share vectors
across replicas use Postgres pgvector instead: set `ROS_VECTOR_BACKEND=pgvector` (step 4) and
install the extension **once** (user-run — modifies the DB):

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- run against the Postgres service
```

---

## 3. Create the api service (+ volume)

```bash
cd agent-studio-backend
railway add --service api                       # add an empty service to the linked project
railway volume add --service api --mount-path /app/.data   # REQUIRED: master.key (+ chroma)
```

The build is driven by the repo's `railway.json` (Dockerfile `apps/api/Dockerfile`, start command,
healthcheck `/readyz`) — no build config needed in the dashboard.

> **⚠️ Pin `PORT=8000` on the api service.** Railway injects `PORT=8080` by default, so the api
> (`uvicorn --port ${PORT:-8000}`) listens on **8080** — but the web proxies to
> `api.railway.internal:8000` (below). That mismatch means the console silently can't reach the
> api (every call refused; only Railway's own `/readyz` gets through). Set `PORT=8000` on `api` so
> both sides agree, or set the web's `ROS_API_URL` to the api's actual port. Verify with
> `railway logs --service api | grep "Uvicorn running"` → must show `:8000`.

### 3a. api environment variables

Set with `railway variables --service api --set 'KEY=value'` (repeatable). Use Railway
`${{Postgres.*}}` / `${{Redis.*}}` references so rotations propagate.

**Required (app won't work correctly without these):**

| Variable | Value | Notes |
|---|---|---|
| `PORT` | `8000` | pin it — Railway defaults to 8080, breaking the web→api proxy (see ⚠️ above) |
| `ROS_DATABASE_URL` | `postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}` | **must be the `+asyncpg` driver** (SQLAlchemy async) |
| `ROS_CHECKPOINT_BACKEND` | `postgres` | LangGraph checkpointer; falls back to `ROS_DATABASE_URL` |
| `ROS_REDIS_URL` | `redis://default:${{Redis.REDIS_PASSWORD}}@${{Redis.RAILWAY_PRIVATE_DOMAIN}}:6379` | shared rate-limit / idempotency / token-revocation |
| `ROS_JWT_SECRET` | *(generated)* | strong random; rotating it logs everyone out |
| `ROS_BOOTSTRAP_ADMIN_EMAIL` | `admin@yourco.com` | seeded workspace owner |
| `ROS_BOOTSTRAP_ADMIN_PASSWORD` | *(generated)* | first-login password; not `ros-admin` |
| `ROS_PUBLIC_BASE_URL` | `https://<api-domain>` | set after step 5 (domains) |
| `ROS_PUBLIC_CONSOLE_URL` | `https://<web-domain>` | set after step 5 |
| `ROS_CORS_ORIGINS` | `["https://<web-domain>"]` | JSON array or comma list |

**Recommended:**

| Variable | Value | Notes |
|---|---|---|
| `ROS_ENVIRONMENT` | `production` | turns ON the security guard (see §6 — requires `ROS_TRUSTED_HOSTS` + https URLs or the api refuses to boot). Use `development` only to skip the guard. |
| `ROS_TRUSTED_HOSTS` | `["<api-domain>"]` | **required if `ROS_ENVIRONMENT=production`** |
| `ROS_TRUSTED_PROXIES` | `["*"]` | trust Railway's edge for real client IP (rate limits/audit) |
| `ROS_SECRET_KEY` | *(Fernet key)* | **required for >1 api replica** — else each replica auto-generates a different master key and can't decrypt peers' secrets. Single replica may omit (uses the volume file). |
| `ROS_VECTOR_BACKEND` | `chroma` \| `pgvector` | `chroma` uses the volume; `pgvector` needs the extension (§2) |
| `ROS_SERVICE_API_TOKEN` | *(generated, ≥24 chars)* | server-to-server bearer; omit to disable |
| `ROS_LOG_JSON` | `true` | structured logs for Datadog/Loki/ELK |
| `ROS_DEFAULT_MODEL` | e.g. `openai:gpt-4o-mini` | `fake:echo` (default) is offline-only |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | *(provider keys)* | **not** `ROS_`-prefixed — read by the model SDKs. Or configure providers in-app under Settings. |

Example:

```bash
railway variables --service api \
  --set 'PORT=8000' \
  --set 'ROS_DATABASE_URL=postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}' \
  --set 'ROS_CHECKPOINT_BACKEND=postgres' \
  --set 'ROS_REDIS_URL=redis://default:${{Redis.REDIS_PASSWORD}}@${{Redis.RAILWAY_PRIVATE_DOMAIN}}:6379' \
  --set 'ROS_JWT_SECRET=<generated>' \
  --set 'ROS_BOOTSTRAP_ADMIN_EMAIL=admin@yourco.com' \
  --set 'ROS_BOOTSTRAP_ADMIN_PASSWORD=<generated>' \
  --set 'ROS_VECTOR_BACKEND=chroma' \
  --set 'ROS_ENVIRONMENT=development'
```

(Full list of tunables + docs: `apps/api/ros/config.py` and `.env.example`.)

---

## 4. Create the web service

```bash
cd ../agent-studio-frontend
railway add --service web
railway variables --service web \
  --set 'ROS_API_URL=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000'
```

- `ROS_API_URL` — **build-time** proxy target (Dockerfile `ARG ROS_API_URL`; Next.js standalone
  freezes the rewrite destination at build). The api listens on **8000** (`EXPOSE 8000`,
  `uvicorn --port ${PORT:-8000}`); keep it 8000 or match a custom `PORT` you set on api.
- Leave `NEXT_PUBLIC_ROS_API_URL` **unset** — that would send the browser directly to the api
  (cross-origin); unset keeps the same-origin `/api/ros` proxy. Set it only for a split-domain
  SSE setup.

---

## 5. Domains, then backfill the URL vars

```bash
railway domain --service api      # -> https://<api>.up.railway.app
railway domain --service web      # -> https://<web>.up.railway.app
```

Now set the URL-dependent vars to the generated domains (they're circular with step 3):

```bash
railway variables --service api \
  --set 'ROS_PUBLIC_BASE_URL=https://<api-domain>' \
  --set 'ROS_PUBLIC_CONSOLE_URL=https://<web-domain>' \
  --set 'ROS_CORS_ORIGINS=["https://<web-domain>"]'
# if ROS_ENVIRONMENT=production also:
  # --set 'ROS_TRUSTED_HOSTS=["<api-domain>"]'
```

---

## 6. Deploy + verify

Deploy each service from its repo root (`railway.json` drives the Dockerfile build). `-c` streams
build logs then exits; the CLI's log stream may time out — that's cosmetic, poll status after.

```bash
# backend (runs alembic upgrade head on start)
cd ../agent-studio-backend && railway up --service api -c
# frontend (ROS_API_URL is baked here — make sure it's set first)
cd ../agent-studio-frontend && railway up --service web -c
```

Verify:

```bash
railway service list --json          # all four services SUCCESS
railway logs --service api | grep -iE "INSECURE|Unsafe|postgres|sqlite|Application startup"
curl -s -o /dev/null -w "%{http_code}\n" https://<api-domain>/readyz     # 200
```

- The api log should show it connected to Postgres and **no** "INSECURE CONFIG: JWT secret is the
  built-in dev default" line (if you see that, a `ROS_*` var is misnamed — see the banner up top).
- With `ROS_ENVIRONMENT=production`, a misconfig makes the api **refuse to boot** with
  `Unsafe production configuration:` listing the missing vars — fix those and redeploy.
- Log in to `https://<web-domain>` with the bootstrap admin email/password.

---

## 7. Production hardening checklist

The security guard (`config.py::validate_production`) is enforced for any `ROS_ENVIRONMENT` other
than dev/local/test. To pass it:

- [ ] `ROS_ENVIRONMENT=production`
- [ ] `ROS_JWT_SECRET` strong + random (not the dev default)
- [ ] `ROS_AUTH_REQUIRED=true` (default)
- [ ] `ROS_BOOTSTRAP_ADMIN_PASSWORD` ≠ `ros-admin`
- [ ] `ROS_DATABASE_URL` is Postgres (not sqlite)
- [ ] `ROS_CHECKPOINT_BACKEND=postgres`
- [ ] `ROS_EGRESS_BLOCK_PRIVATE=true` (default — SSRF guard)
- [ ] `ROS_TRUSTED_HOSTS` set to the api hostname(s)
- [ ] `ROS_PUBLIC_BASE_URL` / `ROS_PUBLIC_CONSOLE_URL` are `https://`
- [ ] `ROS_SERVICE_API_TOKEN` empty or ≥24 chars
- [ ] `ROS_SECRET_KEY` set (from KMS/Vault) if running >1 api replica

---

## 8. Gotchas (learned the hard way)

- **`FORGE_*` vars are dead.** The env prefix is `ROS_`. Mixing prefixes = app on SQLite +
  default secrets + guard skipped, but `/readyz` still returns 200. Grep the startup log for
  `INSECURE CONFIG` / `sqlite` to catch it.
- **IPv6-only private network (#55).** Railway's private domains are IPv6-only. The api's
  `ROS_PREFER_IPV4_EGRESS=true` (default) installs an IPv4-first DNS patch that can break reaching
  `*.railway.internal`. If the api can't connect to Postgres/Redis on the private net, set
  **`ROS_PREFER_IPV4_EGRESS=false`** (and use private domains, not public, for DB/Redis).
- **`ROS_API_URL` is build-time.** Set it on `web` before `railway up`; changing it later needs a
  rebuild, not just a restart.
- **api volume is mandatory.** Without `/app/.data`, the Fernet master key regenerates every deploy
  and previously-encrypted secrets become undecryptable. (Multi-replica: use `ROS_SECRET_KEY`.)
- **Next.js version / vuln scanner.** Railway's build-time scanner blocks known-vuln deps; keep
  `next` current (≥ 14.2.35 for CVE-2025-55184/67779).
- **No BuildKit `--mount=type=cache`** in the api Dockerfile — Railway's Metal builder rejects
  cache mounts without its cacheKey-prefixed id.
- **Migrations** run automatically on api start (`alembic upgrade head`, with retry). No manual step.
- **`ROS_DEFAULT_MODEL=fake:echo`** is an offline echo — set a real provider model + key for real runs.
```
