"""Add analyze_cursor_segment_index column to jobs table.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-11 00:00:00.000000

Notes
-----
Adds a nullable INTEGER column ``analyze_cursor_segment_index`` to the ``jobs``
table. This is used by the segment-based Pipeline v1 to track how many segments
have been processed in the ``baseline_segments`` stage and in the
``analyze_optional`` job type.

The addition is guarded: if the column already exists (greenfield deployment
using init_db()) the upgrade step is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    if "analyze_cursor_segment_index" not in existing_columns:
        op.add_column(
            "jobs",
            sa.Column("analyze_cursor_segment_index", sa.Integer, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    if "analyze_cursor_segment_index" in existing_columns:
        op.drop_column("jobs", "analyze_cursor_segment_index")
