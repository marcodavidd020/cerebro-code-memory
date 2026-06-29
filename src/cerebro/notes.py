"""Decision log (plan layer 4): cross-session memory of the *why*.

The structural index recovers WHAT exists for free (tree-sitter). What a new chat
can never recover by reading code is the WHY: decisions, domain rules, and gotchas
("QR_MANUAL = merchant confirms payment by hand", "Seller was refactored to
Organization"). Sessions persist these with cerebro_note and retrieve them with
cerebro_recall. Stored in the `notes` table; indexed in `fts` under kind='note'.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(conn, topic: str | None, content: str) -> int:
    cur = conn.execute(
        "INSERT INTO notes(topic, content, created_at) VALUES(?,?,?)",
        (topic or None, content, now_iso()),
    )
    nid = cur.lastrowid
    conn.execute(
        "INSERT INTO fts(path, kind, text) VALUES(?, 'note', ?)",
        (f"note:{nid}", f"{topic or ''}\n{content}"),
    )
    conn.commit()
    return nid


def list_recent(conn, limit: int = 10):
    return conn.execute(
        "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def recall(conn, query: str = "", limit: int = 10):
    """Return notes matching `query` (by topic/content meaning), or the most recent
    notes when `query` is empty."""
    if not query.strip():
        return list_recent(conn, limit)

    ids: list[int] = []
    try:
        rows = conn.execute(
            "SELECT path FROM fts WHERE fts MATCH ? AND kind='note' "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        ids = [int(r["path"].split(":", 1)[1]) for r in rows]
    except sqlite3.OperationalError:
        pass
    if not ids:  # FTS syntax rejected the query, or no hits — fall back to LIKE
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT id FROM notes WHERE topic LIKE ? OR content LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    notes = conn.execute(
        f"SELECT * FROM notes WHERE id IN ({placeholders})", ids
    ).fetchall()
    order = {nid: i for i, nid in enumerate(ids)}  # preserve match ranking
    return sorted(notes, key=lambda r: order.get(r["id"], 1 << 30))
