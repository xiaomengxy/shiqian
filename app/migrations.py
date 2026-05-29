from __future__ import annotations

import json

from sqlalchemy import Engine, inspect, text


def ensure_runtime_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if "bookmarks" in tables:
            _ensure_bookmarks(conn)
        if "parse_jobs" in tables:
            _ensure_parse_jobs(conn)


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
            title, summary, content_type, source_domain, directory_id, status, created_at, updated_at
        )
        SELECT
            id, url, canonical_url, NULL, 'url', COALESCE(url, ''), ?,
            title, summary, content_type, source_domain, directory_id, status, created_at, updated_at
        FROM bookmarks
        """,
        (json.dumps([]),),
    )
    conn.exec_driver_sql("DROP TABLE bookmarks")
    conn.exec_driver_sql("ALTER TABLE bookmarks_new RENAME TO bookmarks")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_bookmarks_source_domain ON bookmarks (source_domain)")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _ensure_parse_jobs(conn) -> None:
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(parse_jobs)").fetchall()}
    if "source_type" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN source_type VARCHAR(20) NOT NULL DEFAULT 'url'")
    if "raw_input" not in columns:
        conn.exec_driver_sql("ALTER TABLE parse_jobs ADD COLUMN raw_input TEXT NOT NULL DEFAULT ''")
        conn.exec_driver_sql("UPDATE parse_jobs SET raw_input = input_url WHERE raw_input = ''")

