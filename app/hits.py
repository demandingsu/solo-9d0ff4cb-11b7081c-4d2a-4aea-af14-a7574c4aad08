"""检索库命中维护。

一条命中 = 同一页上「同名 + 同一校订稿区间」的全部来源合并结果。来源有两类：

- ``annotation``：迁移后可用（auto / confirmed）的旧标注；
- ``supplement``：编辑在校订稿上补录的人名。

命中编号（``hits.id``）持久化在库中，重启后可按编号回查。每次本页的标注
状态或补录发生变化后调用 :func:`reconcile`，规则是：

1. 新位置已有同名同区间命中 → 旧标注/补录并入该命中，**沿用既有编号**；
2. 来源从旧命中移走；旧命中若已无来源则删除；
3. 补录更正时若要「编号随补录移动」，调用方应在 reconcile 前直接改
   ``hits`` 行（见 :func:`move_supplement_hit`），本模块只负责来源归属。

所有函数都假定调用方已持有写锁 / 处于事务中（reconcile、move 需写事务）。
"""

from __future__ import annotations

import sqlite3


def _now(conn: sqlite3.Connection) -> str:
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]


def reconcile(conn: sqlite3.Connection, page_id: int) -> None:
    """按当前可用标注与补录重建本页的命中—来源归属（保持既有编号）。"""
    anns = conn.execute(
        "SELECT id, name, new_start, new_end FROM annotations"
        " WHERE page_id = ? AND status IN ('auto','confirmed')"
        " AND new_start IS NOT NULL ORDER BY seq, id", (page_id,)).fetchall()
    sups = conn.execute(
        "SELECT id, name, new_start, new_end FROM supplements"
        " WHERE page_id = ? ORDER BY id", (page_id,)).fetchall()

    # 目标归属：key=(name, start, end) -> [(source_type, source_id), ...]
    desired: dict[tuple[str, int, int], list[tuple[str, int]]] = {}
    for a in anns:
        key = (a["name"], a["new_start"], a["new_end"])
        desired.setdefault(key, []).append(("annotation", a["id"]))
    for s in sups:
        key = (s["name"], s["new_start"], s["new_end"])
        desired.setdefault(key, []).append(("supplement", s["id"]))

    hit_by_key: dict[tuple[str, int, int], int] = {}
    for h in conn.execute(
            "SELECT id, name, new_start, new_end FROM hits WHERE page_id = ?",
            (page_id,)):
        hit_by_key[(h["name"], h["new_start"], h["new_end"])] = h["id"]

    links: dict[tuple[str, int], int] = {}
    for row in conn.execute(
            "SELECT hs.source_type, hs.source_id, hs.hit_id FROM hit_sources hs"
            " JOIN hits h ON h.id = hs.hit_id WHERE h.page_id = ?", (page_id,)):
        links[(row["source_type"], row["source_id"])] = row["hit_id"]

    desired_links: set[tuple[str, int]] = set()
    changed_hits: set[int] = set()
    for key, sources in desired.items():
        name, ns, ne = key
        hit_id = hit_by_key.get(key)
        if hit_id is None:
            cur = conn.execute(
                "INSERT INTO hits (page_id, name, new_start, new_end)"
                " VALUES (?,?,?,?)", (page_id, name, ns, ne))
            hit_id = int(cur.lastrowid)
            hit_by_key[key] = hit_id
        for source_type, source_id in sources:
            link = (source_type, source_id)
            desired_links.add(link)
            old_hit = links.get(link)
            if old_hit == hit_id:
                continue
            if old_hit is None:
                conn.execute(
                    "INSERT INTO hit_sources (hit_id, source_type, source_id)"
                    " VALUES (?,?,?)", (hit_id, source_type, source_id))
            else:
                conn.execute(
                    "UPDATE hit_sources SET hit_id = ?"
                    " WHERE source_type = ? AND source_id = ?",
                    (hit_id, source_type, source_id))
                changed_hits.add(old_hit)
            changed_hits.add(hit_id)

    for link, old_hit in links.items():
        if link not in desired_links:
            conn.execute(
                "DELETE FROM hit_sources WHERE source_type = ? AND source_id = ?",
                link)
            changed_hits.add(old_hit)

    conn.execute(
        "DELETE FROM hits WHERE page_id = ? AND id NOT IN"
        " (SELECT hit_id FROM hit_sources)", (page_id,))
    if changed_hits:
        now = _now(conn)
        for hit_id in changed_hits:
            conn.execute("UPDATE hits SET updated_at = ? WHERE id = ?",
                         (now, hit_id))


def backfill_all() -> None:
    """启动时把既有页纳入命中索引。

    升级前的页还没有命中行，需要回填；已经有命中的页不重建，避免重启时
    重新分配运行中已确立的编号（例如更正后已迁到新位置的编号）。
    """
    from . import db

    conn = db.connect()
    try:
        page_ids = [r["id"] for r in conn.execute(
            "SELECT p.id FROM pages p WHERE NOT EXISTS"
            " (SELECT 1 FROM hits h WHERE h.page_id = p.id)")]
        for page_id in page_ids:
            conn.execute("BEGIN IMMEDIATE")
            reconcile(conn, page_id)
            conn.commit()
    finally:
        conn.close()


def move_supplement_hit(conn: sqlite3.Connection, old_hit_id: int,
                        name: str, start: int, end: int) -> None:
    """原位只有这一条补录时，让命中编号随补录移到新位置。

    目标位置若已有同一命中行（不会发生：调用方已校验新位置无同名命中），
    或同页恰有另一个来源也在移动目标处，则由随后的 reconcile 合并。
    """
    conn.execute(
        "UPDATE hits SET name = ?, new_start = ?, new_end = ?, updated_at = ?"
        " WHERE id = ?", (name, start, end, _now(conn), old_hit_id))


def supplement_hit_id(conn: sqlite3.Connection,
                      supplement_id: int) -> int | None:
    row = conn.execute(
        "SELECT hit_id FROM hit_sources"
        " WHERE source_type = 'supplement' AND source_id = ?",
        (supplement_id,)).fetchone()
    return row["hit_id"] if row else None


def hit_sources(conn: sqlite3.Connection, hit_id: int) -> list[dict]:
    """命中来源列表（旧标注在前），供接口输出。"""
    rows = conn.execute(
        "SELECT hs.source_type, hs.source_id, a.status AS annotation_status"
        " FROM hit_sources hs"
        " LEFT JOIN annotations a ON hs.source_type = 'annotation'"
        " AND a.id = hs.source_id"
        " WHERE hs.hit_id = ?"
        " ORDER BY CASE hs.source_type WHEN 'annotation' THEN 0 ELSE 1 END,"
        " hs.source_id", (hit_id,)).fetchall()
    return [{
        "source_type": r["source_type"],
        "source_id": r["source_id"],
        "source_status": (r["annotation_status"] if r["source_type"] == "annotation"
                          else "supplement"),
    } for r in rows]


def page_hits(conn: sqlite3.Connection, page_id: int) -> list[dict]:
    """按页供数：本页全部命中（含来源明细）。"""
    hits = conn.execute(
        "SELECT h.*, p.page_ref, p.new_text FROM hits h"
        " JOIN pages p ON p.id = h.page_id WHERE h.page_id = ?"
        " ORDER BY h.new_start, h.new_end, h.name, h.id",
        (page_id,)).fetchall()
    out = []
    for h in hits:
        out.append({
            "hit_id": h["id"],
            "page_id": h["page_id"],
            "page_ref": h["page_ref"],
            "name": h["name"],
            "start": h["new_start"],
            "end": h["new_end"],
            "excerpt": h["new_text"][h["new_start"]:h["new_end"]],
            "sources": hit_sources(conn, h["id"]),
            "updated_at": h["updated_at"],
        })
    return out


def get_hit(conn: sqlite3.Connection, hit_id: int) -> dict | None:
    h = conn.execute(
        "SELECT h.*, p.page_ref, p.new_text FROM hits h"
        " JOIN pages p ON p.id = h.page_id WHERE h.id = ?", (hit_id,)).fetchone()
    if h is None:
        return None
    return {
        "hit_id": h["id"],
        "page_id": h["page_id"],
        "page_ref": h["page_ref"],
        "name": h["name"],
        "start": h["new_start"],
        "end": h["new_end"],
        "excerpt": h["new_text"][h["new_start"]:h["new_end"]],
        "sources": hit_sources(conn, h["id"]),
        "updated_at": h["updated_at"],
    }


def find_hit(conn: sqlite3.Connection, page_id: int, name: str,
             start: int, end: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM hits WHERE page_id = ? AND name = ?"
        " AND new_start = ? AND new_end = ?", (page_id, name, start, end)
    ).fetchone()
    return row["id"] if row else None


def supplement_out(conn: sqlite3.Connection, supplement_id: int) -> dict | None:
    s = conn.execute(
        "SELECT s.*, p.new_text FROM supplements s"
        " JOIN pages p ON p.id = s.page_id WHERE s.id = ?",
        (supplement_id,)).fetchone()
    if s is None:
        return None
    return {
        "id": s["id"],
        "page_id": s["page_id"],
        "name": s["name"],
        "new_start": s["new_start"],
        "new_end": s["new_end"],
        "excerpt": s["new_text"][s["new_start"]:s["new_end"]],
        "hit_id": supplement_hit_id(conn, s["id"]),
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
    }
