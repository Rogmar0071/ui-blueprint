"""Add analyze_repo job type support — no new columns needed; documents artifact types.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-12 00:00:00.000000

Notes
-----
The analyze_repo job type reuses all existing Job columns.
This migration is a no-op DDL migration that documents the new artifact types
repo_zip and repo_analysis_md in the ops_events log schema comment only.
No table changes required.
"""
from __future__ import annotations

revision: str = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass  # No DDL changes needed; analyze_repo reuses existing columns.


def downgrade() -> None:
    pass
