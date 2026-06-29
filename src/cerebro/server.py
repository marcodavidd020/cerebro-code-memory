"""Cerebro MCP server.

Exposes the persistent code-knowledge brain as MCP tools so any chat session can
*query* what was already understood instead of re-reading folders. Output is kept
deliberately compact (token-cheap) — that is the whole point of the project.
"""
from __future__ import annotations

import os
import posixpath
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config as cfg
from . import apiroutes, callgraph, db, embeddings, gitsync, graph, indexer, insights, notes, summaries, views

mcp = FastMCP("cerebro")

_CONFIG: cfg.Config | None = None
_CONN = None


def _ctx():
    global _CONFIG, _CONN
    if _CONFIG is None:
        _CONFIG = cfg.Config.load()
    if _CONN is None:
        _CONN = db.connect(_CONFIG.db_path)
    return _CONFIG, _CONN


_DEP_CAP = 15  # max dependency paths listed before collapsing to "(+N more)"
_SYM_CAP = 40  # max symbols listed for one file


def _join_capped(items, n: int = _DEP_CAP) -> str:
    items = list(items)
    if len(items) <= n:
        return ", ".join(items)
    return ", ".join(items[:n]) + f", … (+{len(items) - n} more)"


def _empty_index_hint(conn) -> str | None:
    n = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    if n == 0:
        return "Index is empty. Run cerebro_reindex() first to build the map."
    return None


def _resolve_path(config, conn, path: str) -> str:
    """Normalize whatever path form the model passes (absolute, ./relative, or
    already repo-relative) to the canonical repo-relative key the brain is indexed
    by. Without this, a summary recorded under an absolute path is invisible to a
    later cerebro_get using a relative one — which silently breaks cross-session
    persistence."""
    p = Path(path)
    if p.is_absolute():
        try:
            norm = p.resolve().relative_to(config.root.resolve()).as_posix()
        except ValueError:
            norm = path  # outside the indexed root; leave as given
    else:
        norm = posixpath.normpath(path.lstrip("/"))
    if conn.execute("SELECT 1 FROM files WHERE path=?", (norm,)).fetchone():
        return norm
    # Fallback: a unique indexed file whose path ends with what was given.
    rows = conn.execute(
        "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\'",
        ("%/" + norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["path"]
    return norm


@mcp.tool()
def cerebro_map(top: int = 30) -> str:
    """Cheap whole-project overview: file/language counts and the most important
    modules ranked by dependency centrality (PageRank). Call this FIRST in a new
    session instead of exploring folders."""
    config, conn = _ctx()
    return views.map_text(conn, config.root, top)


@mcp.tool()
def cerebro_get(path: str) -> str:
    """Everything Cerebro knows about a file WITHOUT reading it: cached summary
    (with staleness flag), defined symbols, and dependency edges."""
    config, conn = _ctx()
    path = _resolve_path(config, conn, path)
    file_row = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
    if file_row is None:
        return f"'{path}' is not in the index. It may be new — run cerebro_reindex()."

    out = [f"# {path}  ({file_row['lang'] or 'other'})"]
    abs_path = config.root / path
    live_hash = indexer.file_hash(abs_path) if abs_path.exists() else None
    s = summaries.get(conn, path, current_hash=live_hash)
    if s:
        flag = "  ⚠ STALE (file changed since summary)" if s["stale"] else ""
        out.append(f"\nSummary:{flag}\n{s['summary_en']}")
    else:
        out.append("\nSummary: (none yet — record one with cerebro_record)")

    syms = db.symbols_for(conn, path)
    if syms:
        out.append("\nSymbols:")
        out += [
            f"  L{r['line']:<5} {r['kind']:<8} {r['signature'] or r['name']}"
            for r in syms[:_SYM_CAP]
        ]
        if len(syms) > _SYM_CAP:
            out.append(f"  … (+{len(syms) - _SYM_CAP} more symbols)")

    deps = graph.dependencies(conn, path)
    dependents = graph.dependents(conn, path)
    if deps:
        out.append("\nImports (depends on): " + _join_capped(deps))
    if dependents:
        out.append("Imported by (impact if changed): " + _join_capped(dependents))
    return "\n".join(out)


@mcp.tool()
def cerebro_search(query: str, limit: int = 15) -> str:
    """Find relevant files. When the semantic index is built it ranks by meaning
    (intent), so phrase queries naturally ("where do we validate stock at
    checkout?"); it also includes keyword/symbol matches. Returns paths to
    cerebro_get()."""
    config, conn = _ctx()
    hint = _empty_index_hint(conn)
    if hint:
        return hint

    sem = embeddings.search(config, conn, query, limit=limit)
    lines, seen = [], set()
    for path, name, line, score in sem:
        seen.add(path)
        if name:
            loc = f"{path}:{line}" if line else path
            lines.append(f"~{score:.2f} {loc} — {name}")
        else:
            s = summaries.get(conn, path)
            snip = f" — {s['summary_en'][:90]}" if s else ""
            lines.append(f"~{score:.2f} {path}{snip}")
    for r in db.search(conn, query, limit=limit):
        if r["path"] in seen:
            continue
        lines.append(f"[{r['kind']}] {r['path']} — {r['snip']}")

    if not lines:
        return f"No matches for '{query}'."
    header = "(semantic + keyword)\n" if sem else ""
    return header + "\n".join(lines[:limit])


@mcp.tool()
def cerebro_record(path: str, summary: str, model: str = "") -> str:
    """Leave a trace: store your English understanding of a file so future
    sessions reuse it instead of re-analyzing. Write 1-3 dense sentences in
    English describing what the file does and its role."""
    config, conn = _ctx()
    path = _resolve_path(config, conn, path)
    res = summaries.record(conn, path, summary, model or None)
    if not res["indexed"]:
        return (
            f"Recorded summary for '{path}', but it is not in the index yet "
            f"(staleness tracking disabled until cerebro_reindex())."
        )
    return f"Recorded summary for '{path}'."


@mcp.tool()
def cerebro_stale() -> str:
    """What the index no longer trusts: files changed/added/deleted on disk since
    the last reindex, plus summaries whose source file has changed."""
    config, conn = _ctx()
    disk = indexer.disk_state(config)
    d = indexer.diff(conn, disk)
    stale_sum = summaries.stale_summaries(conn)
    if not any([d["new"], d["changed"], d["deleted"], stale_sum]):
        return "Everything is fresh. No reindex needed."
    parts = []
    if d["changed"]:
        parts.append("Changed on disk:\n  " + "\n  ".join(d["changed"]))
    if d["new"]:
        parts.append("New (not indexed):\n  " + "\n  ".join(d["new"]))
    if d["deleted"]:
        parts.append("Deleted (still in index):\n  " + "\n  ".join(d["deleted"]))
    if stale_sum:
        parts.append("Summaries now stale:\n  " + "\n  ".join(stale_sum))
    parts.append("\nRun cerebro_reindex() to refresh the structural index.")
    return "\n\n".join(parts)


@mcp.tool()
def cerebro_sync() -> str:
    """Catch changes made outside Claude Code (branch switch, git pull, edits in the
    raw editor) and reindex only the affected files. Works across nested repos."""
    config, conn = _ctx()
    r = gitsync.sync(config, conn)
    if not r.get("git"):
        return "No git repo found under the root — nothing to sync."
    return f"Git sync: {r['changed']} changed files reindexed across {r['repos']} repo(s)."


@mcp.tool()
def cerebro_reindex(paths: list[str] | None = None) -> str:
    """Refresh the static index (symbols, dependency edges, hashes). Only
    changed/new/deleted files are reprocessed. Pass `paths` to limit scope."""
    config, conn = _ctx()
    result = indexer.reindex(config, conn, paths=paths)
    return (
        f"Reindexed. {result['indexed']} files processed "
        f"(new={result['new']}, changed={result['changed']}, "
        f"deleted={result['deleted']}), {result['total_files']} total."
    )


@mcp.tool()
def cerebro_note(content: str, topic: str = "") -> str:
    """Record a decision, domain rule, or gotcha — the *why* that reading code can
    never recover (e.g. 'QR_MANUAL = merchant confirms payment by hand', 'Seller was
    refactored to Organization'). Future sessions retrieve it with cerebro_recall.
    Keep `content` to 1-3 sentences; `topic` is an optional short tag."""
    _, conn = _ctx()
    nid = notes.add(conn, topic or None, content)
    return f"Recorded note #{nid}" + (f" on '{topic}'." if topic else ".")


@mcp.tool()
def cerebro_recall(query: str = "", limit: int = 10) -> str:
    """Recall decisions/rules/gotchas recorded by past sessions BEFORE re-deriving
    them. Pass a query to search by meaning of topic/content, or leave empty for the
    most recent notes."""
    _, conn = _ctx()
    return views.recall_text(conn, query, limit)


@mcp.tool()
def cerebro_impact(path: str) -> str:
    """Transitive blast radius: every file that directly OR indirectly imports
    `path`. Use before changing a widely-used file to see what could break."""
    config, conn = _ctx()
    path = _resolve_path(config, conn, path)
    r = insights.impact(conn, path)
    if r is None:
        return f"'{path}' is not in the index."
    if r["total"] == 0:
        return f"Nothing imports '{path}' — changing it has no in-repo dependents."
    spread = ", ".join(f"{n} at depth {d}" for d, n in r["by_distance"].items())
    out = [
        f"Changing '{path}' transitively affects {r['total']} files ({spread}).",
        "\nDirect importers (" + str(len(r["direct"])) + "): " + _join_capped(r["direct"], 20),
    ]
    deeper = [p for p in r["all"] if p not in set(r["direct"])]
    if deeper:
        out.append("Further downstream: " + _join_capped(deeper, 20))
    return "\n".join(out)


@mcp.tool()
def cerebro_cycles() -> str:
    """Find circular import groups (files that mutually depend on each other) — an
    architecture smell worth breaking. Returns each cycle's members."""
    _, conn = _ctx()
    r = insights.cycles(conn)
    cs = r["cycles"]
    note = f" (barrel/index files ignored: {r['barrels_ignored']})" if r["barrels_ignored"] else ""
    if not cs:
        return f"No genuine circular import cycles found. 🎉{note}"
    out = [f"{r['total']} circular dependency group(s){note}, tightest first:"]
    for i, c in enumerate(cs, 1):
        tag = f"{c['length']}-file cycle" if c["size"] == c["length"] else f"{c['size']}-file tangle"
        out.append(f"\n{i}. {tag}:")
        out.append("   " + " → ".join(c["cycle"]))
    return "\n".join(out)


@mcp.tool()
def cerebro_callers(name: str) -> str:
    """Find every call site of a function / method / class by NAME across the repo
    (symbol-level call graph; name-resolved, so it may include same-named symbols).
    Use to see who actually uses a symbol before you change it."""
    _, conn = _ctx()
    r = callgraph.callers(conn, name)
    if not r["defined_in"] and r["count"] == 0:
        return f"No symbol named '{name}' found in the index."
    out = []
    if r["defined_in"]:
        out.append(f"'{name}' defined in: " + _join_capped(r["defined_in"], 10))
    if r["count"] == 0:
        out.append("No call sites found in the repo.")
        return "\n".join(out)
    out.append(f"\n{r['count']} call site(s):")
    out += [f"  {sym or '(module scope)'} @ {path}:{line}" for path, sym, line in r["sites"][:60]]
    if r["count"] > 60:
        out.append(f"  … (+{r['count'] - 60} more)")
    return "\n".join(out)


@mcp.tool()
def cerebro_calls(path: str) -> str:
    """List the internal functions/methods a file calls — its outgoing call edges
    (name-resolved). External library calls are omitted."""
    config, conn = _ctx()
    path = _resolve_path(config, conn, path)
    r = callgraph.calls_from(conn, path)
    if r["count"] == 0:
        return f"No internal calls found from '{path}'."
    out = [f"'{path}' makes {r['count']} internal call(s):"]
    out += [f"  {sym or '(module scope)'} → {dst}()  L{line}" for sym, dst, line in r["calls"][:80]]
    if r["count"] > 80:
        out.append(f"  … (+{r['count'] - 80} more)")
    return "\n".join(out)


@mcp.tool()
def cerebro_orphans(prefix: str = "") -> str:
    """List code files that nothing imports — dead-code candidates. Framework
    entrypoints (modules, controllers, pages, configs, tests) are listed
    separately since they're loaded by convention, not by import."""
    _, conn = _ctx()
    r = insights.orphans(conn, prefix or None)
    if not r["dead"] and not r["entrypoints"]:
        return "No orphan files found in scope."
    out = []
    if r["dead"]:
        out.append(f"Dead-code candidates ({len(r['dead'])}) — imported by nothing, not entrypoints:")
        out += [f"  {p}" for p in r["dead"][:60]]
        if len(r["dead"]) > 60:
            out.append(f"  … (+{len(r['dead']) - 60} more)")
    else:
        out.append("No dead-code candidates — every non-entrypoint file is imported.")
    out.append(f"\n(Plus {len(r['entrypoints'])} unimported framework entrypoints, excluded.)")
    return "\n".join(out)


@mcp.tool()
def cerebro_dead_symbols(prefix: str = "") -> str:
    """List unused-export candidates: functions/classes/methods whose name is
    referenced nowhere in the indexed code, inside files that ARE imported (the
    symbol-level dead code that cerebro_orphans, which works per-file, can't see).
    Heuristic — confirm before deleting: dynamic access (obj['x'], string-based
    DI) and reflection can make a used symbol look dead."""
    _, conn = _ctx()
    r = insights.dead_symbols(conn, prefix or None)
    if not r["dead"]:
        return "No unused-export candidates found in scope."
    out = [f"Unused-export candidates ({r['total']}) — defined but referenced nowhere:"]
    for d in r["dead"][:60]:
        out.append(f"  {d['path']}:{d['line']}  {d['kind']} {d['name']}")
    if r["total"] > 60:
        out.append(f"  … (+{r['total'] - 60} more)")
    out.append("\n(Heuristic: verify — dynamic/reflective access can hide a real use.)")
    return "\n".join(out)


@mcp.tool()
def cerebro_endpoints(query: str = "") -> str:
    """Backend HTTP endpoints (NestJS routes) the project exposes — the front↔back
    boundary that `import` edges miss. Search by path / method / handler (e.g.
    'POST carts', 'promotions', 'findActive') to answer 'where is this endpoint
    handled?' without grepping decorators."""
    config, conn = _ctx()
    eps = apiroutes.find(config, conn, query)
    if not eps:
        scope = f" matching '{query}'" if query else ""
        return f"No backend endpoints{scope} found (scans *.controller.ts NestJS routes)."
    cap = 60
    out = [f"{len(eps)} endpoint(s)" + (f" matching '{query}'" if query else "") + ":"]
    for e in eps[:cap]:
        h = f"  ({e['handler']})" if e["handler"] else ""
        out.append(f"  {e['method']:6} {e['path']}  → {e['file']}:{e['line']}{h}")
    if len(eps) > cap:
        out.append(f"  … (+{len(eps) - cap} more)")
    return "\n".join(out)


def map_main():
    """`cerebro-map` entry point: print the project map (read-only). Used by the
    Claude Code session-start hook to inject the overview into a new session."""
    print(cerebro_map())


def recall_main():
    """`cerebro-recall` entry point: print recalled notes (recent if no query).
    Used by the session-start hook to surface decisions in a new session."""
    import sys

    print(cerebro_recall(" ".join(sys.argv[1:])))


def main():
    # Allow pointing the server at a specific repo via env without changing cwd.
    if os.environ.get("CEREBRO_ROOT"):
        _ctx()
    mcp.run()


if __name__ == "__main__":
    main()
