"""Add analyze_options JSON column to jobs table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-11 00:00:00.000000

Notes
-----
Adds a single nullable JSON column ``analyze_options`` to the ``jobs`` table.
This stores the user-supplied per-job analysis options (e.g. optional analyses
such as keyframe selection) and is read by the worker pipeline at every step.

The column addition is guarded: if it already exists (deployment ran init_db()
with the updated model) the addition is skipped.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        # Greenfield: init_db() will create the table with all columns.
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    if "analyze_options" not in existing_columns:
        op.add_column("jobs", sa.Column("analyze_options", sa.JSON, nullable=True))


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    if "analyze_options" in existing_columns:
        op.drop_column("jobs", "analyze_options")
