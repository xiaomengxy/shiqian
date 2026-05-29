"""Add app config table.

Revision ID: 0003_app_config
Revises: 0002_mixed_items
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_app_config"
down_revision = "0002_mixed_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column("key", sa.String(length=120), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_config")

