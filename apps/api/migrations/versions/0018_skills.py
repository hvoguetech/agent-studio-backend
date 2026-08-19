"""skills: the agent skill library (Anthropic Agent Skills pattern)

Creates the `skills` table (tenant/project-scoped SKILL.md content attached to deep_agent nodes
by id). `create_all` builds it on fresh dev DBs; this migration is the controlled path for managed
Postgres. Idempotent: no-op if the table already exists.

Revision ID: 0018_skills
Revises: 0017_run_agent_id
Create Date: 2026-08-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_skills"
down_revision = "0017_run_agent_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "skills" in insp.get_table_names():
        return
    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        # 64 = the Agent Skills spec's max name length; the name is also the mounted directory.
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("files", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("tenant_id", "project_id", "name", name="uq_skill_tenant_project_name"),
    )
    op.create_index("ix_skills_tenant_id", "skills", ["tenant_id"])
    op.create_index("ix_skills_project_id", "skills", ["project_id"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "skills" in insp.get_table_names():
        op.drop_table("skills")
