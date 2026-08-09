"""artifacts: durable agent/tool artifact records (WS7)

Creates the `artifacts` table (tenant/project/run-scoped pointer to an object-store blob).
`create_all` builds it on fresh dev DBs; this migration is the controlled path for managed
Postgres. Idempotent: no-op if the table already exists.

Revision ID: 0012_artifacts
Revises: 0011_run_reclaim
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_artifacts"
down_revision = "0011_run_reclaim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "artifacts" in insp.get_table_names():
        return
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "content_type", sa.String(length=255), nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("filename", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_artifacts_tenant_id", "artifacts", ["tenant_id"])
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_index("ix_artifacts_sha256", "artifacts", ["sha256"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "artifacts" in insp.get_table_names():
        op.drop_table("artifacts")
