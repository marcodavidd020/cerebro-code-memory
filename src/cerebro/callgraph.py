"""Symbol-level call graph queries over the `calls` table.

Resolution is by NAME (tree-sitter, no type inference), so results can include
same-named symbols from different files — fast and dependency-free, but not as
precise as an LSP. Good for "who calls X?" and "what does this file call?".
"""
from __future__ import annotations


def callers(conn, name: str, limit: int = 300) -> dict:
    defined = sorted(
        {r["file_path"] for r in conn.execute(
            "SELECT file_path FROM symbols WHERE name=?", (name,))}
    )
    rows = conn.execute(
        "SELECT src_path, src_symbol, line FROM calls WHERE dst_name=? "
        "ORDER BY src_path, line",
        (name,),
    ).fetchall()
    sites = [(r["src_path"], r["src_symbol"], r["line"]) for r in rows]
    return {"name": name, "defined_in": defined, "count": len(sites), "sites": sites[:limit]}


def calls_from(conn, path: str, limit: int = 300) -> dict:
    """Internal calls a file makes — callees that resolve to a symbol defined
    somewhere in the repo (external library calls are dropped)."""
    defined = {r["name"] for r in conn.execute("SELECT DISTINCT name FROM symbols")}
    rows = conn.execute(
        "SELECT DISTINCT src_symbol, dst_name, line FROM calls WHERE src_path=? "
        "ORDER BY line",
        (path,),
    ).fetchall()
    internal = [
        (r["src_symbol"], r["dst_name"], r["line"])
        for r in rows
        if r["dst_name"] in defined
    ]
    return {"path": path, "count": len(internal), "calls": internal[:limit]}
