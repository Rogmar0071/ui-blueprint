"""Add composite index on artifacts(folder_id, type).

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        # Table does not exist yet; init_db() will create it with any indices.
        return
    op.create_index(
        "ix_artifacts_folder_id_type",
        "artifacts",
        ["folder_id", "type"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        return
    op.drop_index("ix_artifacts_folder_id_type", table_name="artifacts")
