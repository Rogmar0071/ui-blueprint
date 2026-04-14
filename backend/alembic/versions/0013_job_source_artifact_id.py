"""Add source_artifact_id column to jobs table.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-14 00:00:01.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("jobs")}
    if "source_artifact_id" not in existing_columns:
        op.add_column("jobs", sa.Column("source_artifact_id", sa.Uuid(), nullable=True))
    if "ix_jobs_source_artifact_id" not in existing_indexes:
        op.create_index("ix_jobs_source_artifact_id", "jobs", ["source_artifact_id"])


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("jobs")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("jobs")}
    if "ix_jobs_source_artifact_id" in existing_indexes:
        op.drop_index("ix_jobs_source_artifact_id", table_name="jobs")
    if "source_artifact_id" in existing_columns:
        op.drop_column("jobs", "source_artifact_id")
