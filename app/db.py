"""SQLite 存储层。"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.environ.get(
    "LOCALGAZ_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "localgaz.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_ref   TEXT,
    old_text   TEXT NOT NULL,
    new_text   TEXT NOT NULL,
    lcs_len    INTEGER NOT NULL,          -- 新旧两稿的 LCS 长度
    edit_ops   INTEGER NOT NULL,          -- 插入+删除总次数 = n + m - 2*lcs_len
    version    INTEGER NOT NULL DEFAULT 0, -- 每次确认生效后 +1
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS annotations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id   INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,           -- 提交顺序
    name      TEXT NOT NULL,
    old_start INTEGER NOT NULL,
    old_end   INTEGER NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('auto','pending','confirmed')),
    new_start INTEGER,                    -- pending 时为 NULL
    new_end   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_annotations_page ON annotations(page_id);

CREATE TABLE IF NOT EXISTS confirmations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id       INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    annotation_id INTEGER NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
    new_start     INTEGER NOT NULL,
    new_end       INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_confirmations_page ON confirmations(page_id);

-- 编辑在校订稿上补录的人名（新稿区间）。同一页、同名、同区间只存一行。
CREATE TABLE IF NOT EXISTS supplements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    new_start  INTEGER NOT NULL,
    new_end    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (page_id, name, new_start, new_end)
);
CREATE INDEX IF NOT EXISTS idx_supplements_page ON supplements(page_id);

-- 检索库命中：一页上同名、同新区间的一组来源合并为一条命中。
-- 编号持久化，重启后可按编号回查；后来可用的旧标注并入既有命中时沿用编号。
CREATE TABLE IF NOT EXISTS hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    new_start  INTEGER NOT NULL,
    new_end    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (page_id, name, new_start, new_end)
);
CREATE INDEX IF NOT EXISTS idx_hits_page ON hits(page_id);

-- 命中的来源：旧标注（annotations）或编辑补录（supplements）。
-- 每条来源最多归属一条命中。
CREATE TABLE IF NOT EXISTS hit_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hit_id      INTEGER NOT NULL REFERENCES hits(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('annotation','supplement')),
    source_id   INTEGER NOT NULL,
    UNIQUE (source_type, source_id)
);
CREATE INDEX IF NOT EXISTS idx_hit_sources_hit ON hit_sources(hit_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
