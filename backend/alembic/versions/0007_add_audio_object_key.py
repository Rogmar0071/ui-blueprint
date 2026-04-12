"""Add audio_object_key to folders table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-12 00:00:00.000000

Notes
-----
Adds a nullable TEXT column ``audio_object_key`` to the ``folders`` table.
This stores the object-key for an audio-only recording associated with
the folder.

The addition is guarded: if the column already exists (greenfield deployment
using init_db()) the upgrade step is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "folders" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("folders")}
    if "audio_object_key" not in existing_columns:
        op.add_column(
            "folders",
            sa.Column("audio_object_key", sa.Text, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "folders" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("folders")}
    if "audio_object_key" in existing_columns:
        op.drop_column("folders", "audio_object_key")
