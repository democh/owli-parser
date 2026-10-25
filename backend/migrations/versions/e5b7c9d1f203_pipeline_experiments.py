"""pipeline experiments table

Revision ID: e5b7c9d1f203
Revises: c4f8a2be91d7
Create Date: 2026-10-17

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e5b7c9d1f203"
down_revision = "c4f8a2be91d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_experiments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_name", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("variant_a_yaml", sa.String(), nullable=False),
        sa.Column("variant_b_yaml", sa.String(), nullable=False),
        sa.Column("variant_a_digest", sa.String(length=64), nullable=False),
        sa.Column("variant_b_digest", sa.String(length=64), nullable=False),
        sa.Column("traffic_percent", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "pipeline_name", name="uq_pipeline_experiment"),
    )


def downgrade() -> None:
    op.drop_table("pipeline_experiments")
