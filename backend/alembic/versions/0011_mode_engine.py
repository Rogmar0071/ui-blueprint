"""Mode Engine Enforcement + Mutation Simulation V2 — schema additions.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-15 00:00:00.000000

Changes
-------
1. Add nullable ``selected_modes`` JSON column to ``global_chat_messages``
   so each persisted message records the active mode-engine stack.

2. Create ``mode_engine_audit_log`` table for full audit traceability:
   - Core audit fields  (MODE_ENGINE_ENFORCEMENT_PATCH_V1 audit_layer)
   - Mutation V2 fields (MODE_ENGINE_MUTATION_SIMULATION_V2 audit_layer)

All DDL changes are guarded: idempotent on greenfield deployments where
``init_db()`` has already created the tables via SQLModel metadata.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ------------------------------------------------------------------
    # 1. Add selected_modes to global_chat_messages
    # ------------------------------------------------------------------
    if "global_chat_messages" in existing_tables:
        existing_cols = {
            col["name"]
            for col in inspector.get_columns("global_chat_messages")
        }
        if "selected_modes" not in existing_cols:
            op.add_column(
                "global_chat_messages",
                sa.Column("selected_modes", sa.JSON, nullable=True),
            )

    # ------------------------------------------------------------------
    # 2. Create mode_engine_audit_log
    # ------------------------------------------------------------------
    if "mode_engine_audit_log" not in existing_tables:
        op.create_table(
            "mode_engine_audit_log",
            sa.Column("id", sa.Uuid, primary_key=True, nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=True,
                index=True,
            ),
            # Core audit fields
            sa.Column("user_intent", sa.Text, nullable=False),
            sa.Column("selected_modes", sa.JSON, nullable=True),
            sa.Column("transformed_prompt", sa.Text, nullable=True),
            sa.Column("raw_ai_response", sa.Text, nullable=True),
            sa.Column("validation_results", sa.JSON, nullable=True),
            sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("final_output", sa.Text, nullable=True),
            # Mutation Simulation V2 extended fields
            sa.Column("mutation_contract", sa.JSON, nullable=True),
            sa.Column("simulation_results", sa.JSON, nullable=True),
            sa.Column("enforcement_results", sa.JSON, nullable=True),
            sa.Column("build_status", sa.Text, nullable=True),
            sa.Column("commit_id", sa.Text, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "mode_engine_audit_log" in existing_tables:
        op.drop_table("mode_engine_audit_log")

    if "global_chat_messages" in existing_tables:
        existing_cols = {
            col["name"]
            for col in inspector.get_columns("global_chat_messages")
        }
        if "selected_modes" in existing_cols:
            op.drop_column("global_chat_messages", "selected_modes")
