"""runs: crash-recovery lease columns (A/C9)

Adds owner_id / heartbeat_at / reclaim_attempts to `runs` so a driver can lease a running row
(heartbeat) and the reclaim supervisor can detect + re-drive orphaned runs whose driver died.
`create_all` builds these on fresh dev DBs; this migration covers managed Postgres and
pre-existing tables. Idempotent: each column is added only when absent, back-filling safely
(nullable, and reclaim_attempts defaults to 0).

Revision ID: 0011_run_reclaim
Revises: 0010_toolset_exposed
Create Date: 2026-08-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_run_reclaim"
down_revision = "0010_toolset_exposed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("runs")}
    if "owner_id" not in cols:
        op.add_column("runs", sa.Column("owner_id", sa.String(length=80), nullable=True))
    if "heartbeat_at" not in cols:
        op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    if "reclaim_attempts" not in cols:
        op.add_column(
            "runs",
            sa.Column("reclaim_attempts", sa.Integer(), server_default="0", nullable=False),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("runs")}
    for name in ("reclaim_attempts", "heartbeat_at", "owner_id"):
        if name in cols:
            op.drop_column("runs", name)
