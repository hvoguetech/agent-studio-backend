"""provisioned_backends: per-end-user isolation (forUser)

Adds a nullable `end_user_id` to `provisioned_backends` so a resource can be owned by
(agent_id, end_user_id) - that end user's private substrate - with NULL meaning agent-shared. At
runtime an agent gets shared ∪ this-user's resources. Idempotent add_column.

Revision ID: 0016_provisioned_backend_end_user
Revises: 0015_run_executor
Create Date: 2026-08-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_provisioned_backend_end_user"
down_revision = "0015_run_executor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "provisioned_backends" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("provisioned_backends")}
    if "end_user_id" not in cols:
        op.add_column("provisioned_backends", sa.Column("end_user_id", sa.String(length=128), nullable=True))
        op.create_index("ix_provisioned_backends_end_user_id", "provisioned_backends", ["end_user_id"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "provisioned_backends" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("provisioned_backends")}
    if "end_user_id" in cols:
        op.drop_index("ix_provisioned_backends_end_user_id", table_name="provisioned_backends")
        op.drop_column("provisioned_backends", "end_user_id")
