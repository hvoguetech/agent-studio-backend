"""Project budget + allowed-models admission checks (finding f).

`project.config` (packages/schemas/ros/project.json) can declare:
  - `allowed_models`: an allow-list of model refs the project may run.
  - `budgets.monthly_usd_cap`: a calendar-month spend ceiling for the project.
  - `budgets.max_usd_per_run`: the per-run reservation counted against the monthly cap so a
    concurrent burst can't each read a stale "already spent" total and blow past it.

Only `budgets.max_tokens_per_run` was consumed before (by the run budget middleware); this
module adds the monthly cost cap + model allow-list at RUN ADMISSION.

INTEGRATION (run admission lives in the off-limits services/quota.py): call
`enforce_project_budget(...)` from `RunService.create_run` (services/runs.py, right after
`check_run_quota` at ~line 176), passing the resolved model, OR inside each admission wrapper
(routers/project_run.py, routers/runs.py, routers/embed_public.py). It raises `BudgetExceeded`
(map to HTTP 402/429) or `ModelNotAllowed` (map to HTTP 400/403). It's a no-op unless the
project configures a cap / allow-list, so wiring it is always safe.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from ros.models import Project, ProvisionedBackend, Run


def collect_workflow_models(executable: dict | None) -> set[str]:
    """Every chat-model ref declared in a workflow's nodes - agent/llm/classifier `config.model`
    plus any nested middleware `model`/`models` (model_fallback, summarization, tool-selector).
    Only these node types carry a `model` key in the schema; embedders live under different keys
    (`embedding_model`), so they're never collected. Backs the publish-time allow-list check."""
    models: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            m = obj.get("model")
            if isinstance(m, str) and m:
                models.add(m)
            for x in obj.get("models") or []:
                if isinstance(x, str) and x:
                    models.add(x)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for node in (executable or {}).get("nodes") or []:
        if isinstance(node, dict):
            walk(node.get("config"))
    return models


def disallowed_workflow_models(project_config: dict | None, executable: dict | None) -> list[str]:
    """The workflow's chat models that the project's `allowed_models` forbids (sorted). Empty
    when the project sets no allow-list (no-op) or every model is permitted - so the publish
    route can enforce it unconditionally. Mirrors enforce_project_budget's per-run model check
    across ALL per-node models at publish time."""
    allowed = (project_config or {}).get("allowed_models") or []
    if not allowed:
        return []
    return sorted(m for m in collect_workflow_models(executable) if m not in allowed)


class BudgetError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BudgetExceeded(BudgetError):
    """The project's monthly spend cap would be exceeded by admitting this run."""


class ModelNotAllowed(BudgetError):
    """The requested model is not in the project's allowed_models list."""


def _month_start_utc() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


async def enforce_project_budget(
    session, tenant_id: str, project_id: str, *, model: str | None = None,
    executable: dict | None = None,
) -> None:
    """Raise if a model isn't allowed or the project's monthly USD cap would be exceeded.

    No-op when the project configures neither an allow-list nor a monthly cap. Loads the project
    by (tenant, id); a missing project is treated as unconfigured (no enforcement). When
    `executable` is given, EVERY per-node model in the workflow is checked against the allow-list
    at admission (not just the project default), so a run can't smuggle in a disallowed model."""
    project = (
        await session.execute(
            select(Project).where(Project.tenant_id == tenant_id, Project.id == project_id)
        )
    ).scalar_one_or_none()
    if project is None:
        return
    cfg = project.config or {}

    allowed = cfg.get("allowed_models") or []
    # Validate the run's model (or the project default) against the allow-list.
    candidate = model or cfg.get("default_model")
    if allowed and candidate and candidate not in allowed:
        raise ModelNotAllowed(f"model {candidate!r} is not in this project's allowed_models")
    # Validate every per-node model in the workflow too (closes the admission gap where only the
    # project default was checked; per-node models were previously gated only at publish).
    if allowed and executable is not None:
        bad = disallowed_workflow_models(cfg, executable)
        if bad:
            raise ModelNotAllowed(
                f"workflow uses models not in this project's allowed_models: {', '.join(bad)}"
            )

    budgets = cfg.get("budgets") or {}
    cap = budgets.get("monthly_usd_cap")
    try:
        cap = float(cap or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap <= 0:
        return

    try:
        reserve = float(budgets.get("max_usd_per_run") or 0)
    except (TypeError, ValueError):
        reserve = 0.0

    spent = (
        await session.execute(
            select(func.coalesce(func.sum(Run.total_cost_usd), 0.0)).where(
                Run.tenant_id == tenant_id,
                Run.project_id == project_id,
                Run.created_at >= _month_start_utc(),
                Run.status != "error",
            )
        )
    ).scalar() or 0.0

    if float(spent) + reserve >= cap:
        raise BudgetExceeded(
            f"project monthly budget reached (${float(spent):.2f} + ${reserve:.2f} reserved "
            f">= ${cap:.2f})"
        )


class ProvisionNotAllowed(BudgetError):
    """Provisioning a new managed backend would exceed the project's max_backends cap."""


async def enforce_provision_admission(session, tenant_id: str, project_id: str) -> None:
    """Raise ProvisionNotAllowed if the project is at/over its managed-backend cap. Gates
    agent/operator-initiated provisioning (services/backend_provisioning.py) the same way
    enforce_project_budget gates run spend. No-op unless `project.config.budgets.max_backends` is
    set. Counts the project's non-deleted ProvisionedBackend rows."""
    project = (
        await session.execute(
            select(Project).where(Project.tenant_id == tenant_id, Project.id == project_id)
        )
    ).scalar_one_or_none()
    if project is None:
        return
    budgets = (project.config or {}).get("budgets") or {}
    raw = budgets.get("max_backends")
    if raw is None:
        return
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        return
    count = (
        await session.execute(
            select(func.count()).select_from(ProvisionedBackend).where(
                ProvisionedBackend.tenant_id == tenant_id,
                ProvisionedBackend.project_id == project_id,
                ProvisionedBackend.status != "deleted",
            )
        )
    ).scalar() or 0
    if count >= cap:
        raise ProvisionNotAllowed(f"project managed-backend cap reached ({count} >= {cap})")
