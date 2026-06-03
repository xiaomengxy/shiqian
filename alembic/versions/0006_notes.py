"""Add notes table.

Revision ID: 0006_notes
Revises: 0005_bookmark_open_tracking
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_notes"
down_revision = "0005_bookmark_open_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default="Untitled"),
        sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("directory_id", sa.Integer()),
        sa.Column("folder_path", sa.String(length=600), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text()),
        sa.Column("source_type", sa.String(length=40), nullable=False, server_default="manual"),
        sa.Column("source_id", sa.String(length=160)),
        sa.Column("source_meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("deleted_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["directory_id"], ["directories.id"]),
    )
    op.create_index("ix_notes_source_id", "notes", ["source_id"])
    op.create_index("ix_notes_directory_id", "notes", ["directory_id"])
    op.create_index("ix_notes_folder_path", "notes", ["folder_path"])


def downgrade() -> None:
    op.drop_index("ix_notes_folder_path", table_name="notes")
    op.drop_index("ix_notes_directory_id", table_name="notes")
    op.drop_index("ix_notes_source_id", table_name="notes")
    op.drop_table("notes")
