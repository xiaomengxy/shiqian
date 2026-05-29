"""Support mixed bookmark items.

Revision ID: 0002_mixed_items
Revises: 0001_initial
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_mixed_items"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bookmarks") as batch:
        batch.alter_column("url", existing_type=sa.Text(), nullable=True)
        batch.alter_column("canonical_url", existing_type=sa.Text(), nullable=True)
        batch.add_column(sa.Column("content_hash", sa.String(length=80), unique=True))
        batch.add_column(sa.Column("source_type", sa.String(length=20), nullable=False, server_default="url"))
        batch.add_column(sa.Column("raw_input", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("keywords", sa.JSON(), nullable=False, server_default="[]"))
    op.execute("UPDATE bookmarks SET raw_input = COALESCE(url, '') WHERE raw_input = ''")
    with op.batch_alter_table("parse_jobs") as batch:
        batch.add_column(sa.Column("source_type", sa.String(length=20), nullable=False, server_default="url"))
        batch.add_column(sa.Column("raw_input", sa.Text(), nullable=False, server_default=""))
    op.execute("UPDATE parse_jobs SET raw_input = input_url WHERE raw_input = ''")


def downgrade() -> None:
    with op.batch_alter_table("parse_jobs") as batch:
        batch.drop_column("raw_input")
        batch.drop_column("source_type")
    with op.batch_alter_table("bookmarks") as batch:
        batch.drop_column("keywords")
        batch.drop_column("raw_input")
        batch.drop_column("source_type")
        batch.drop_column("content_hash")
        batch.alter_column("canonical_url", existing_type=sa.Text(), nullable=False)
        batch.alter_column("url", existing_type=sa.Text(), nullable=False)

