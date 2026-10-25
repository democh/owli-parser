"""chat runs parent link (multi-turn)

Revision ID: f4c1d8e27a90
Revises: e5b7c9d1f203
Create Date: 2026-10-17

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f4c1d8e27a90"
down_revision = "e5b7c9d1f203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_runs", sa.Column("parent_run_id", sa.Uuid(), nullable=True))
    op.create_index("ix_chat_runs_parent_run_id", "chat_runs", ["parent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_runs_parent_run_id", table_name="chat_runs")
    op.drop_column("chat_runs", "parent_run_id")
