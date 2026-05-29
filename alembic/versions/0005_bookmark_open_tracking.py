"""Add bookmark open tracking.

Revision ID: 0005_bookmark_open_tracking
Revises: 0004_job_progress
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_bookmark_open_tracking"
down_revision = "0004_job_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bookmarks") as batch:
        batch.add_column(sa.Column("opened_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_opened_at", sa.DateTime()))


def downgrade() -> None:
    with op.batch_alter_table("bookmarks") as batch:
        batch.drop_column("last_opened_at")
        batch.drop_column("opened_count")
