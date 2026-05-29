"""Add parse job progress fields.

Revision ID: 0004_job_progress
Revises: 0003_app_config
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_job_progress"
down_revision = "0003_app_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("parse_jobs") as batch:
        batch.add_column(sa.Column("stage", sa.String(length=120), nullable=False, server_default="等待处理"))
        batch.add_column(sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("updated_at", sa.DateTime()))
    op.execute(
        "UPDATE parse_jobs SET progress_percent = CASE WHEN status = 'completed' THEN 100 WHEN status = 'failed' THEN 100 ELSE 0 END"
    )
    op.execute("UPDATE parse_jobs SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("parse_jobs") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("progress_percent")
        batch.drop_column("stage")
