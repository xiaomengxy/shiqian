from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Engine, inspect, text


def ensure_runtime_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if "bookmarks" in tables:
            _ensure_bookmarks(conn)
        if {"bookmarks", "directories"}.issubset(tables):
            _merge_uncategorized_directory(conn)
        if "parse_jobs" in tables:
            _ensure_parse_jobs(conn)
        if "app_config" not in tables:
            _ensure_app_config(conn)


def _ensure_bookmarks(conn) -> None:
    columns = {row[1]: row for row in conn.exec_driver_sql("PRAGMA table_info(bookmarks)").fetchall()}
    needs_rebuild = (
        "source_type" not in columns
        or "raw_input" not in columns
        or "keywords" not in columns
        or "content_hash" not in columns
        or columns.get("url", [None, None, None, 0])[3] == 1
        or columns.get("canonical_url", [None, None, None, 0])[3] == 1
    )
    if not needs_rebuild:
        column_names = set(columns)
        if "opened_count" not in column_names:
            conn.exec_driver_sql("ALTER TABLE bookmarks ADD COLUMN opened_count INTEGER NOT NULL DEFAULT 0")
        if "last_opened_at" not in column_names:
            conn.exec_driver_sql("ALTER TABLE bookmarks ADD COLUMN last_opened_at DATETIME")
        if "deleted_at" not in column_names:
            conn.exec_driver_sql("ALTER TABLE bookmarks ADD COLUMN deleted_at DATETIME")
        if "parsed_at" not in column_names:
            conn.exec_driver_sql("ALTER TABLE bookmarks ADD COLUMN parsed_at DATETIME")
            conn.exec_driver_sql("UPDATE bookmarks SET parsed_at = created_at WHERE parsed_at IS NULL")
        return

    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.exec_driver_sql(
        """
        CREATE TABLE bookmarks_new (
            id INTEGER NOT NULL PRIMARY KEY,
            url TEXT,
            canonical_url TEXT UNIQUE,
            content_hash VARCHAR(80) UNIQUE,
            source_type VARCHAR(20) NOT NULL DEFAULT 'url',
            raw_input TEXT NOT NULL DEFAULT '',
            keywords JSON NOT NULL DEFAULT '[]',
            title VARCHAR(500) NOT NULL,
            summary TEXT NOT NULL,
            content_type VARCHAR(80) NOT NULL,
            source_domain VARCHAR(255) NOT NULL,
            directory_id INTEGER,
            status VARCHAR(40) NOT NULL,
            opened_count INTEGER NOT NULL DEFAULT 0,
            last_opened_at DATETIME,
            deleted_at DATETIME,
            parsed_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY(directory_id) REFERENCES directories (id)
        )
        """
    )
    conn.exec_driver_sql(
        """
        INSERT INTO bookmarks_new (
            id, url, canonical_url, content_hash, source_type, raw_input, keywords,
            title, summary, content_type, source_domain, directory_id, status,
            opened_count, last_opened_at, deleted_at, parsed_at, created_at, updated_at
        )
        SELECT
            id, url, canonical_url, NULL, 'url', COALESCE(url, ''), ?,
            title, summary, content_type, source_domain, directory_id, status,
            0, NULL, NULL, created_at, created_at, updated_at
        FROM bookmarks
        """,
        (json.dumps([]),),
    )
    conn.exec_driver_sql("DROP TABLE bookmarks")
    conn.exec_driver_sql("ALTER TABLE bookmarks_new RENAME TO bookmarks")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_bookmarks_source_domain ON bookmarks (source_domain)")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _merge_uncategorized_directory(conn) -> None:
    row = conn.exec_driver_sql("SELECT id FROM directories WHERE path = '未分类'").fetchone()
    if row is None:
        return
    directory_id = row[0]
    conn.exec_driver_sql("UPDATE bookmarks SET directory_id = NULL WHERE directory_id = ?", (directory_id,))
    child = conn.exec_driver_sql("SELECT id FROM directories WHERE parent_id = ? LIMIT 1", (directory_id,)).fetchone()
    if child is None:
        conn.exec_driver_sql("DELETE FROM directories WHERE id = ?", (directory_id,))


def _ensure_parse_jobs(conn) -> None:
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(parse_jobs)").fetchall()}
    if "source_type" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN source_type VARCHAR(20) NOT NULL DEFAULT 'url'")
    if "raw_input" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN raw_input TEXT NOT NULL DEFAULT ''")
        conn.exec_driver_sql("UPDATE parse_jobs SET raw_input = input_url WHERE raw_input = ''")
    if "stage" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN stage VARCHAR(120) NOT NULL DEFAULT '等待处理'")
        conn.exec_driver_sql("UPDATE parse_jobs SET stage = '等待确认' WHERE status = 'completed'")
        conn.exec_driver_sql("UPDATE parse_jobs SET stage = '处理失败' WHERE status = 'failed'")
    if "progress_percent" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN progress_percent INTEGER NOT NULL DEFAULT 0")
        conn.exec_driver_sql(
            "UPDATE parse_jobs SET progress_percent = CASE WHEN status = 'completed' THEN 100 WHEN status = 'failed' THEN 100 ELSE 0 END"
        )
    if "updated_at" not in columns:
        now = datetime.utcnow().isoformat(sep=" ")
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN updated_at DATETIME")
        conn.exec_driver_sql("UPDATE parse_jobs SET updated_at = COALESCE(created_at, ?)", (now,))
    if "deleted_at" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN deleted_at DATETIME")
    if "parsed_at" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN parsed_at DATETIME")
        conn.exec_driver_sql(
            "UPDATE parse_jobs SET parsed_at = COALESCE(updated_at, created_at) WHERE status IN ('completed', 'saved')"
        )
    if "group_confidence" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN group_confidence FLOAT")
        conn.exec_driver_sql("UPDATE parse_jobs SET group_confidence = 1.0 WHERE group_confidence IS NULL")
    if "group_reason" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN group_reason TEXT")
        conn.exec_driver_sql("UPDATE parse_jobs SET group_reason = '历史任务' WHERE group_reason IS NULL")
    if "grouping_source" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN grouping_source VARCHAR(40)")
        conn.exec_driver_sql("UPDATE parse_jobs SET grouping_source = 'legacy' WHERE grouping_source IS NULL")


def _ensure_app_config(conn) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS app_config (
            key VARCHAR(120) NOT NULL PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at DATETIME NOT NULL
        )
        """
    )
