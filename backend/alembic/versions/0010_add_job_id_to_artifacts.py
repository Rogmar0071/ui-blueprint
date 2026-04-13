"""Add job_id to artifacts table.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-13 00:00:00.000000

Notes
-----
Adds a nullable UUID column ``job_id`` to the ``artifacts`` table so that
each artifact can be traced back to the specific job that produced it.

The column is nullable for backward-compatibility: artifacts created before
this migration (or by the clip-upload route, which creates a 'clip' artifact
directly) will have job_id = NULL.

The addition is guarded: if the column already exists (greenfield deployment
using init_db()) the upgrade step is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("artifacts")}
    if "job_id" not in existing_columns:
        op.add_column(
            "artifacts",
            sa.Column("job_id", sa.Uuid, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("artifacts")}
    if "job_id" in existing_columns:
        op.drop_column("artifacts", "job_id")
