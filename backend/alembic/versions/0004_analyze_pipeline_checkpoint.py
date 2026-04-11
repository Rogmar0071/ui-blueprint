"""Add analyze pipeline checkpoint columns to jobs table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-11 00:00:00.000000

Notes
-----
Adds four nullable columns to the ``jobs`` table to persist the state of
the resumable Pipeline v1 analyze job:

    analyze_stage               TEXT        – current stage (prepare/frames/summarize)
    analyze_cursor_frame_index  INTEGER     – frames extracted so far
    analyze_total_frames        INTEGER     – estimated total frames to extract
    analyze_clip_object_key     TEXT        – R2 object key of the source clip

Each column is guarded: if it already exists (e.g. a deployment that ran
init_db() with the new model) the column addition is skipped.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_NEW_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("analyze_stage", sa.Text()),
    ("analyze_cursor_frame_index", sa.Integer()),
    ("analyze_total_frames", sa.Integer()),
    ("analyze_clip_object_key", sa.Text()),
]


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        # Greenfield: init_db() will create the full table with all columns.
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    for col_name, col_type in _NEW_COLUMNS:
        if col_name not in existing_columns:
            op.add_column("jobs", sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    for col_name, _ in reversed(_NEW_COLUMNS):
        if col_name in existing_columns:
            op.drop_column("jobs", col_name)
