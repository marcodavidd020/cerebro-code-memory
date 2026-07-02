"""Cached English summaries (plan layer 2) and summary-staleness.

A summary is tied to the file version it described. Staleness is judged by the
file's `struct_hash` (symbol signatures + imports) rather than raw bytes: a 1-3
sentence role summary rarely changes when only comments, whitespace, or function
bodies change, so byte-level comparison over-invalidates and wastes re-reads and
re-summaries. The byte `source_hash` is still stored and used as a fallback for
summaries written before struct_hash existed (NULL on those rows).
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(conn, path: str, summary: str, model: str | None = None) -> dict:
    row = conn.execute(
        "SELECT hash, struct_hash FROM files WHERE path=?", (path,)
    ).fetchone()
    source_hash = row["hash"] if row else None
    struct_hash = row["struct_hash"] if row else None
    conn.execute(
        """INSERT INTO summaries(path, summary_en, model, source_hash, struct_hash, updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             summary_en=excluded.summary_en, model=excluded.model,
             source_hash=excluded.source_hash, struct_hash=excluded.struct_hash,
             updated_at=excluded.updated_at""",
        (path, summary, model, source_hash, struct_hash, now_iso()),
    )
    conn.execute("DELETE FROM fts WHERE path=? AND kind='summary'", (path,))
    conn.execute(
        "INSERT INTO fts(path, kind, text) VALUES(?, 'summary', ?)", (path, summary)
    )
    conn.commit()
    return {"path": path, "indexed": source_hash is not None}


def get(conn, path: str, current_hash: str | None = None,
        current_struct: str | None = None) -> dict | None:
    """Look up a cached summary and judge staleness. Callers that already know the
    live file state can pass `current_hash` (on-disk byte hash) and `current_struct`
    (on-disk structure hash) to compare against disk directly; anything not passed
    is read from the last-indexed `files` row, which only reflects post-reindex
    changes. Staleness prefers the structure comparison and falls back to bytes when
    either side lacks a struct_hash (e.g. a summary from before the column existed)."""
    row = conn.execute("SELECT * FROM summaries WHERE path=?", (path,)).fetchone()
    if not row:
        return None
    if current_hash is None or current_struct is None:
        file_row = conn.execute(
            "SELECT hash, struct_hash FROM files WHERE path=?", (path,)
        ).fetchone()
        if current_hash is None:
            current_hash = file_row["hash"] if file_row else None
        if current_struct is None:
            current_struct = file_row["struct_hash"] if file_row else None
    if row["struct_hash"] is not None and current_struct is not None:
        stale = row["struct_hash"] != current_struct
    else:  # legacy fallback: byte-level comparison
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
    """Summaries whose source file changed *structurally* since the summary was
    written (signatures or imports), by comparing struct_hash. Falls back to the
    byte source_hash when either side has no struct_hash (pre-migration rows)."""
    rows = conn.execute(
        """SELECT s.path FROM summaries s JOIN files f ON f.path = s.path
           WHERE CASE
             WHEN s.struct_hash IS NOT NULL AND f.struct_hash IS NOT NULL
               THEN s.struct_hash != f.struct_hash
             ELSE s.source_hash IS NOT NULL AND s.source_hash != f.hash
           END
           ORDER BY s.path"""
    ).fetchall()
    return [r["path"] for r in rows]
