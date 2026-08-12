"""api_keys: governed-subject fields (capability allow-list + spend cap)

Extends `api_keys` so a key doubles as a governed subject (the agent-profile merge): `capabilities`
(default-deny allow-list) + `budget` (spend cap). The resources a key owns are ProvisionedBackend
rows where agent_id == key.id. Idempotent add_column.

Revision ID: 0014_apikey_governed_subject
Revises: 0013_provisioned_backend
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_apikey_governed_subject"
down_revision = "0013_provisioned_backend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "api_keys" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("api_keys")}
    if "capabilities" not in cols:
        op.add_column("api_keys", sa.Column("capabilities", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
    if "budget" not in cols:
        op.add_column("api_keys", sa.Column("budget", sa.JSON(), server_default=sa.text("'{}'"), nullable=False))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "api_keys" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("api_keys")}
    if "budget" in cols:
        op.drop_column("api_keys", "budget")
    if "capabilities" in cols:
        op.drop_column("api_keys", "capabilities")
