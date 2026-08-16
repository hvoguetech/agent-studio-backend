"""runs: agent_id (governed subject a run acts as)

Adds a nullable `agent_id` to `runs` recording the governed-subject key id (ApiKey.id) when a run
was created by an API-key principal, else NULL (console/JWT, service token, webhook, schedule,
embed, MCP PAT). Prerequisite for injecting the agent's provisioned per-(agent, end_user)
credentials into a live run (2b). Idempotent add_column; back-fills existing rows as NULL.

Revision ID: 0017_run_agent_id
Revises: 0016_provisioned_end_user
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_run_agent_id"
down_revision = "0016_provisioned_end_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("runs")}
    if "agent_id" not in cols:
        op.add_column("runs", sa.Column("agent_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("runs")}
    if "agent_id" in cols:
        op.drop_column("runs", "agent_id")
