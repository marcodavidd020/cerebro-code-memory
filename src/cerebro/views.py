"""FastMCP-free renderers for the read tools.

The MCP SDK (FastMCP → pydantic/starlette/uvicorn) costs ~230ms to import and is
only needed to *serve* MCP. The CLI and the SessionStart hook just need the text
these tools produce, so the rendering logic lives here — importable without pulling
in the server module — and `server.py` delegates to it.
"""
from __future__ import annotations

from . import db, graph, notes, summaries


def map_text(conn, root, top: int = 30) -> str:
    total = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    if total == 0:
        return "Index is empty. Run cerebro_reindex() first to build the map."
    langs = ", ".join(f"{r['lang']}:{r['n']}" for r in db.lang_counts(conn))
    last = conn.execute("SELECT value FROM meta WHERE key='last_reindex'").fetchone()
    lines = [
        f"# Cerebro map — {root}",
        f"{total} files | {langs} | last reindex: {last['value'] if last else 'n/a'}",
        "",
        f"## Top {top} modules by centrality (most depended-upon):",
    ]
    for path, score in graph.rank(conn, top=top):
        s = summaries.get(conn, path)
        note = ""
        if s:
            flag = " (STALE)" if s["stale"] else ""
            note = f" — {s['summary_en'][:90]}{flag}"
        lines.append(f"  {score:.3f}  {path}{note}")
    no_summary = conn.execute(
        "SELECT COUNT(*) AS n FROM files f "
        "LEFT JOIN summaries s ON s.path=f.path WHERE s.path IS NULL"
    ).fetchone()["n"]
    lines.append("")
    lines.append(
        f"{no_summary} files have no summary yet. As you learn a file, call "
        f"cerebro_record(path, summary) so future sessions skip re-reading it."
    )
    return "\n".join(lines)


def recall_text(conn, query: str = "", limit: int = 10) -> str:
    rows = notes.recall(conn, query, limit=limit)
    if not rows:
        return "No notes recorded yet." if not query else f"No notes match '{query}'."
    out = []
    for r in rows:
        head = f"#{r['id']}" + (f" [{r['topic']}]" if r["topic"] else "")
        out.append(f"{head} ({r['created_at']})\n  {r['content']}")
    return "\n".join(out)
