"""provisioned_backends (managed-backend provisioning for agents)

Adds the `provisioned_backends` table: a managed backend (Supabase project / Railway service / Redis
queue) provisioned for a project/agent at runtime. Stores the external provider resource
(`project_ref`) + the `secret://` refs under which its credentials live — never plaintext creds.
See services/backend_provisioning.py.

`create_all` builds it on fresh dev DBs; this is the controlled path for managed Postgres (plus the
RLS policy in infra/postgres_rls.sql, which now includes `provisioned_backends`). Idempotent.

Revision ID: 0013_provisioned_backend
Revises: 0012_artifacts
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_provisioned_backend"
down_revision = "0012_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "provisioned_backends" in insp.get_table_names():
        return
    op.create_table(
        "provisioned_backends",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=30), server_default="supabase", nullable=False),
        sa.Column("project_ref", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="provisioning", nullable=False),
        sa.Column("region", sa.String(length=50), nullable=True),
        sa.Column("endpoint_url", sa.String(length=300), nullable=True),
        sa.Column("secret_refs", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("config", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_provisioned_backends_tenant_id", "provisioned_backends", ["tenant_id"])
    op.create_index("ix_provisioned_backends_project_id", "provisioned_backends", ["project_id"])
    op.create_index("ix_provisioned_backends_agent_id", "provisioned_backends", ["agent_id"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "provisioned_backends" in insp.get_table_names():
        op.drop_table("provisioned_backends")
