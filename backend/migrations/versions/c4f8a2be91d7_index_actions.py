"""index actions table

Revision ID: c4f8a2be91d7
Revises: b1a68170f34d
Create Date: 2026-10-15

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c4f8a2be91d7"
down_revision = "b1a68170f34d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("desired_revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_index_actions_state", "index_actions", ["state", "created_at"])
    op.create_index("ix_index_actions_chunk", "index_actions", ["chunk_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_index_actions_chunk", table_name="index_actions")
    op.drop_index("ix_index_actions_state", table_name="index_actions")
    op.drop_table("index_actions")
