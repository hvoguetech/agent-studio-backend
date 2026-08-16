"""Starter provisioning templates (#6 slice 1).

A template is a named bundle of resource specs so a user asks for a *stack*, not N providers. Kinds
are the CANONICAL provider kinds (railway-postgres | railway-storage | queue | supabase | railway) so
each provisioned row's `provider` maps to the standard runtime env var via SECRET_ENV_VARS /
ENDPOINT_ENV_VARS (e.g. railway-postgres -> DATABASE_URL). Provisioning a template loops
`backend_provisioning.provision_resource` per resource, all sharing one (agent_id, end_user_id).

Kept as a small in-code catalog (no DB): templates are platform config, not tenant data.
"""

from __future__ import annotations

from ros.services.backend_provisioning import is_enabled

# id -> {title, resources: [{kind, spec?}]}. `db` defaults to Railway Postgres (Railway-only default).
TEMPLATES: dict[str, dict] = {
    "db": {
        "title": "Postgres",
        "resources": [{"kind": "railway-postgres"}],
    },
    "db+storage": {
        "title": "Postgres + object storage",
        "resources": [{"kind": "railway-postgres"}, {"kind": "railway-storage"}],
    },
    "db+storage+queue": {
        "title": "Full stack (Postgres + storage + queue)",
        "resources": [{"kind": "railway-postgres"}, {"kind": "railway-storage"}, {"kind": "queue"}],
    },
}


def get_template(template_id: str) -> dict | None:
    """The template's definition (title + resources), or None if unknown."""
    return TEMPLATES.get(template_id)


def resources_for(template_id: str) -> list[dict] | None:
    """The [{kind, spec}] resource list for a template, or None if unknown."""
    tmpl = TEMPLATES.get(template_id)
    if tmpl is None:
        return None
    return [{"kind": r["kind"], "spec": dict(r.get("spec") or {})} for r in tmpl["resources"]]


def list_templates() -> list[dict]:
    """The catalog, each annotated with whether every resource's provider is configured on this
    deploy (`enabled`) and the per-kind enabled flags — so the console can disable a template whose
    providers aren't set up."""
    out: list[dict] = []
    for tid, tmpl in TEMPLATES.items():
        kinds = [r["kind"] for r in tmpl["resources"]]
        providers = [{"kind": k, "enabled": is_enabled(k)} for k in kinds]
        out.append({
            "id": tid,
            "title": tmpl["title"],
            "resources": kinds,
            "providers": providers,
            "enabled": all(p["enabled"] for p in providers),
        })
    return out
