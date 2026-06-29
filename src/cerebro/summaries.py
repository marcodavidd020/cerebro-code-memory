"""Cached English summaries (plan layer 2) and summary-staleness.

A summary is tied to the file version it described via `source_hash`. When the
file's current hash differs, the summary is flagged stale so a session knows to
re-read just that file instead of trusting an outdated trace.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(conn, path: str, summary: str, model: str | None = None) -> dict:
    row = conn.execute("SELECT hash FROM files WHERE path=?", (path,)).fetchone()
    source_hash = row["hash"] if row else None
    conn.execute(
        """INSERT INTO summaries(path, summary_en, model, source_hash, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             summary_en=excluded.summary_en, model=excluded.model,
             source_hash=excluded.source_hash, updated_at=excluded.updated_at""",
        (path, summary, model, source_hash, now_iso()),
    )
    conn.execute("DELETE FROM fts WHERE path=? AND kind='summary'", (path,))
    conn.execute(
        "INSERT INTO fts(path, kind, text) VALUES(?, 'summary', ?)", (path, summary)
    )
    conn.commit()
    return {"path": path, "indexed": source_hash is not None}


def get(conn, path: str, current_hash: str | None = None) -> dict | None:
    """Look up a cached summary. Pass `current_hash` (the live on-disk hash) to
    detect staleness against disk directly; otherwise it is compared against the
    last-indexed hash, which only reflects changes after a reindex."""
    row = conn.execute("SELECT * FROM summaries WHERE path=?", (path,)).fetchone()
    if not row:
        return None
    if current_hash is None:
        file_row = conn.execute(
            "SELECT hash FROM files WHERE path=?", (path,)
        ).fetchone()
        current_hash = file_row["hash"] if file_row else None
    stale = bool(
        row["source_hash"] and current_hash and current_hash != row["source_hash"]
    )
    return {
        "path": path,
        "summary_en": row["summary_en"],
        "model": row["model"],
        "updated_at": row["updated_at"],
        "stale": stale,
    }


def stale_summaries(conn) -> list[str]:
    """Summaries whose source file changed since the summary was written."""
    rows = conn.execute(
        """SELECT s.path FROM summaries s JOIN files f ON f.path = s.path
           WHERE s.source_hash IS NOT NULL AND s.source_hash != f.hash
           ORDER BY s.path"""
    ).fetchall()
    return [r["path"] for r in rows]
