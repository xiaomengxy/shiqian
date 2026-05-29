"""Initial schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "directories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("directories.id")),
        sa.Column("path", sa.String(length=600), nullable=False, unique=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "parse_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("input_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("extracted_json", sa.JSON()),
        sa.Column("suggestion_json", sa.JSON()),
        sa.Column("provider", sa.String(length=40)),
        sa.Column("model", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "bookmarks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=80), nullable=False),
        sa.Column("source_domain", sa.String(length=255), nullable=False),
        sa.Column("directory_id", sa.Integer(), sa.ForeignKey("directories.id")),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("slug", sa.String(length=160), nullable=False, unique=True),
    )
    op.create_table(
        "bookmark_tags",
        sa.Column("bookmark_id", sa.Integer(), sa.ForeignKey("bookmarks.id"), primary_key=True),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("tags.id"), primary_key=True),
    )
    op.create_index("ix_directories_parent_id", "directories", ["parent_id"])
    op.create_index("ix_bookmarks_source_domain", "bookmarks", ["source_domain"])


def downgrade() -> None:
    op.drop_index("ix_bookmarks_source_domain", table_name="bookmarks")
    op.drop_index("ix_directories_parent_id", table_name="directories")
    op.drop_table("bookmark_tags")
    op.drop_table("tags")
    op.drop_table("bookmarks")
    op.drop_table("parse_jobs")
    op.drop_table("directories")

