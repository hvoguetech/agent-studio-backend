# Design: one-command provisioning DX + templates (ROS #6)

Status: **proposed** · Owner: — · Depends on: #2 (agent_id on run) ✅, #3 (runtime_env injection) ✅

## 1. Problem & goal

We shipped the per-end-user isolation **plumbing** (#2→#3): a run carries its governed subject and
the runtime gets that subject's provisioned creds injected (on the master-local path *and* the VM
path). But the capability is **unreachable** — provisioning exists only at the Python service layer
(`ros/services/backend_provisioning.py` + `ros/services/providers/*`). There is **no** HTTP route,
CLI, agent tool, template catalog, or console surface. A customer literally cannot create the
per-(agent, end_user) stack that #3 injects.

**Goal:** make provisioning a frictionless action ("Naïve's one command") and land the starter
templates, so the shipped isolation chain becomes usable and demonstrable.

**The one demo #6 must unlock (local, no VM needed):**
> `provision {postgres}` for an agent → a keyed run → the agent's tool/code reads its **own**
> `DATABASE_URL` → a **second** end user of the same agent gets a **different** DB.

This is provable on the master-local path today because #3 injects there too — so #6 does not depend
on #4 (VM live-verify).

## 2. What already exists (reuse, don't rebuild)

| Piece | Where | Notes |
|-------|-------|-------|
| Provision / teardown | `backend_provisioning.provision_resource / provision_backend / teardown_resource` | Handles secret-ref storage, `ProvisionedBackend` row, rollback-on-failure |
| Providers | `services/providers/`: `supabase`, `railway`, `railway-postgres` (`postgres`), `railway-storage` (`storage`), `queue` | `get_provider(kind)`; each is `is_enabled()`-gated |
| Per-(agent, end_user) isolation | `_secret_name(...)` + `ProvisionedBackend.{agent_id,end_user_id}` | forUser model already wired |
| Runtime injection | `runtime_env` / `resolved_runtime_env` / `runtime_env_for_run` (#3) | consumed by build_compile_context + manifest |
| Capacity cap | `budget.enforce_provision_admission` (per-project `max_backends`) + `ApiKeyService.enforce_capacity` | no-op unless configured |
| Capability gate | `ApiKeyService.allows(key, "backend:provision")` (default-deny) | for agent-key self-provisioning |
| Env var mapping | `SECRET_ENV_VARS` / `ENDPOINT_ENV_VARS` | provider+logical → standard var (DATABASE_URL, …) |

**Missing = the surfaces only.** #6 is thin orchestration + a template catalog + exposure. No new
provider work; no `ProvisionedBackend` schema change (store the template id in `config["template"]`).

## 3. Design

### 3.1 Templates (the "one command" input)
A template is a named bundle of resource specs, so a user asks for a *stack* not N providers.

```jsonc
// ros/services/provision_templates.py — a small in-code catalog (no DB)
{
  "db":            { "title": "Postgres",              "resources": [{"kind": "postgres"}] },
  "db+storage":    { "title": "Postgres + object storage",
                     "resources": [{"kind": "postgres"}, {"kind": "storage"}] },
  "db+storage+queue": { "title": "Full stack",
                     "resources": [{"kind": "postgres"}, {"kind": "storage"}, {"kind": "queue"}] },
}
```
- `list_templates()` → catalog (id, title, resources, which are `is_enabled()` on this deploy).
- Provisioning a template = loop `provision_resource(kind=..., spec=...)` per resource, all tagged
  `config["template"]=<id>` and sharing one `agent_id` (+ optional `end_user_id`). Partial-failure
  policy: **best-effort with rollback of that call** (each `provision_resource` already rolls back its
  own external state); surface per-resource errors in the response (mirrors the storage-bucket
  best-effort precedent). Decision D3 below.

### 3.2 HTTP API (primary surface)
New router `ros/routers/provisioning.py`, prefix `/v1/projects/{project_id}/provisioning`:

| Method | Path | Body | Who | Purpose |
|--------|------|------|-----|---------|
| GET | `/templates` | — | editor+ | List starter templates + which providers are enabled |
| GET | `/resources` | `?agent_id=&end_user_id=` | editor+ | List `ProvisionedBackend` rows (no secret values) |
| POST | `/provision` | `{template?|kind?, agent_id, end_user_id?, spec?, name?}` | editor+ / agent-key w/ cap | Provision a template or single kind |
| DELETE | `/resources/{backend_id}` | — | editor+ | Teardown (`teardown_resource`) |

- **AuthZ:** operator surface gated `require_permission("backend:provision")` (new entry in the
  `PERMISSIONS` registry → editor). An **agent key** self-provisioning is additionally gated by
  `ApiKeyService.allows(key, "backend:provision")` + `enforce_capacity`.
- **Response** reuses the service handle (`backend_id, provider, status, endpoint_url, config,
  public extras`) — **never secret values** (only `secret://` refs, already the service contract).
- **Scoping:** tenant+project from the path/principal; `agent_id` defaults to the caller's governed
  subject when it's an API key (via `governed_subject_id`), else required explicitly.

### 3.3 Agent tool (runtime self-provisioning, forUser)
A builtin tool `provision_backend` so an agent can create a **per-end-user** resource on demand
(e.g. first time it serves a new user): binds `agent_id` = the run's governed subject and
`end_user_id` = the run's bound end user (from `ctx.end_user`), gated by the key's
`backend:provision` capability + capacity. This is the forUser "just-in-time isolation" path.

### 3.4 CLI (`ros provision`)
Thin operator client: `python -m ros.provision --project <id> --agent <key-id> --template db`
→ calls the same service. Low priority vs. API/console; include for the issue's "one command" ask.
(Decision D1: standalone `python -m ros.provision` vs. subcommand of an existing entrypoint.)

### 3.5 Console (frontend repo)
A "Resources" panel on the agent/project: list provisioned resources (provider, template, scope
agent/end-user, status, endpoint), a "Provision" action (pick a template), and teardown — closes the
existing ProvisionedBackend-UI gap. Reads `GET /resources` + `/templates`, posts `/provision`,
deletes `/resources/{id}`. Ships after the backend slice.

## 4. Guardrails
- **No secret leakage:** responses carry only refs/handles; values stay in the secret store.
- **Capacity:** `enforce_provision_admission` (project `max_backends`) + `enforce_capacity` (key
  budget) already enforce caps — wire both into the route/tool.
- **Capability default-deny** for agent-key provisioning.
- **Teardown safety:** `teardown_resource` deletes external project + secrets + row (idempotent).
- **Tenant/project isolation:** all queries scoped; `agent_id`/`end_user_id` scoping matches #3.
- **No silent partial success:** the provision-template response lists each resource's outcome.

## 5. Acceptance (all local / no VM)
1. `GET /templates` lists `db`, `db+storage`, `db+storage+queue` with enabled flags.
2. `POST /provision {template:"db", agent_id:A}` → one `ProvisionedBackend` row, creds as refs.
3. A keyed run for agent A → `ctx.runtime_env["DATABASE_URL"]` resolves to A's DB (via #3).
4. `POST /provision {template:"db", agent_id:A, end_user_id:"bob"}` → a keyed run **as bob** sees
   bob's DB; a run as alice sees the shared/again-different DB — **no cross-user leak**.
5. `DELETE /resources/{id}` → row + secrets gone; a subsequent run injects nothing for it.
6. Capability/capacity denials return 403/402 with clear messages.
(Providers faked in tests as in `test_backend_provisioning.py` — no live Railway/Supabase.)

## 6. Phasing (vertical slices)
- **Slice 1 (core loop): ✅ DONE** — `provision_templates.py` catalog + `routers/provisioning.py`
  (`GET /templates,/resources`, `POST /provision`, `DELETE /resources/{id}`) + `backend:read/provision`
  permissions + `tests/test_provisioning_api.py` (all 6 acceptance checks incl. two-end-user
  isolation, authz, capability + capacity). Proves the wedge end-to-end, locally.
- **Slice 2 (agent-native):** the `provision_backend` agent tool (JIT forUser provisioning).
- **Slice 3 (console):** the Resources panel in `agent-studio-frontend`.
- **Slice 4 (CLI):** `ros provision` thin client.

## 7. Decisions (resolved)
- **D1 — CLI form:** API + console are the primary surfaces; `ros provision` is a thin wrapper over
  the route, deferred to **slice 4** (may be dropped if API+console suffice).
- **D2 — Agent tool:** **slice 2**, after the core loop. Slice 1's HTTP route already proves
  two-end-user isolation via the `end_user_id` param, so the wedge is demonstrable without it.
- **D3 — Template partial-failure:** **best-effort + per-resource report.** Provision what's
  enabled/succeeds, roll back only the failed resource (each `provision_resource` self-rolls-back its
  external state), and return each resource's outcome. Matches the storage-bucket precedent.
- **D4 — Default template/provider:** template `db` → `railway-postgres` (the Railway-only default).
