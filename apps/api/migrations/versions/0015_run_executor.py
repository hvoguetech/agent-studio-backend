"""runs: executor (where a run was driven)

Adds a nullable `executor` JSON to `runs` recording where a run executed - e.g.
{"driver": "freestyle", "vm_id": "vm_9"} for a run driven on a per-run VM. NULL means it ran
locally / on master (the default; no badge). Set at dispatch (the receipt carries the vm_id) and
denormalized onto Trace.meta at finalize for the Traces view. Idempotent add_column.

Revision ID: 0015_run_executor
Revises: 0014_apikey_governed_subject
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_run_executor"
down_revision = "0014_apikey_governed_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("runs")}
    if "executor" not in cols:
        op.add_column("runs", sa.Column("executor", sa.JSON(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("runs")}
    if "executor" in cols:
        op.drop_column("runs", "executor")
