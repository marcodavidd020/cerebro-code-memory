"""Unified `cerebro` command.

With NO subcommand it runs the MCP server (stdio) — so the registration
`... run cerebro` keeps working. With a subcommand it acts as a normal CLI
(`cerebro setup`, `cerebro search ...`, `cerebro graph`, etc.).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as cfg
from . import db


def _ctx():
    config = cfg.Config.load()
    conn = db.connect(config.db_path)
    return config, conn


def _bind_server(config, conn):
    """Point the server tool functions at this config/conn so we can reuse them."""
    from . import server
    server._CONFIG, server._CONN = config, conn
    return server


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


# --- subcommands -------------------------------------------------------------

def cmd_serve(_args):
    from . import server
    server.main()


def cmd_index(args):
    from . import indexer
    config, conn = _ctx()
    if args.paths:
        rels = [r for r in (indexer._to_rel(config, a) for a in args.paths) if r]
        res = indexer.reindex_paths(config, conn, rels)
        res["mode"] = "incremental"
    else:
        res = indexer.reindex(config, conn, force=args.force)
        res["mode"] = "full-force" if args.force else "full"
    print(json.dumps(res, indent=2))


def cmd_search(args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_search(" ".join(args.query)))


def cmd_map(args):
    from . import views
    config, conn = _ctx()
    print(views.map_text(conn, config.root, args.top))


def cmd_impact(args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_impact(args.path))


def cmd_cycles(_args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_cycles())


def cmd_orphans(args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_orphans(args.prefix))


def cmd_orphans_symbols(args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_dead_symbols(args.prefix))


def cmd_callers(args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_callers(args.name))


def cmd_calls(args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_calls(args.path))


def cmd_endpoints(args):
    config, conn = _ctx()
    print(_bind_server(config, conn).cerebro_endpoints(" ".join(args.query)))


def cmd_recall(args):
    from . import views
    config, conn = _ctx()
    print(views.recall_text(conn, " ".join(args.query)))


def cmd_graph(args):
    from . import viz
    config, conn = _ctx()
    out = Path(args.out) if args.out else config.db_path.parent / "cerebro-graph.html"
    out.write_text(viz.graph_html(conn, args.limit, args.prefix), encoding="utf-8")
    print(json.dumps({"html": str(out), "open_with": f"open '{out}'"}))


def cmd_graph_all(_args):
    """Regenerate every scoped graph declared in `.cerebro/graphs.toml`.

    Opt-in per project: with no config file this is an instant no-op, so the
    SessionStart hook can call it for every project without writing graphs into
    repos that never use them. Each [[graph]] entry writes
    `cerebro-graph-<name>.html` (an entry with no name writes the global
    `cerebro-graph.html`); `prefix`/`limit`/`out` mirror the `graph` command.
    """
    from . import viz
    config, conn = _ctx()
    cfg_path = config.db_path.parent / "graphs.toml"
    if not cfg_path.exists():
        print(json.dumps({"skipped": "no .cerebro/graphs.toml"}))
        return
    try:
        import tomllib  # stdlib on Python 3.11+
    except ModuleNotFoundError:
        print(json.dumps({"skipped": "tomllib unavailable (needs Python 3.11+)"}))
        return
    spec = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    written = []
    for g in spec.get("graph", []):
        prefix = g.get("prefix") or None
        limit = int(g.get("limit", 400))
        if g.get("out"):
            out = Path(g["out"])
        elif g.get("name"):
            out = config.db_path.parent / f"cerebro-graph-{g['name']}.html"
        else:
            out = config.db_path.parent / "cerebro-graph.html"
        out.write_text(viz.graph_html(conn, limit, prefix), encoding="utf-8")
        written.append(str(out))
    print(json.dumps({"written": written}))


def cmd_obsidian(args):
    from . import viz
    config, conn = _ctx()
    out = Path(args.out) if args.out else config.db_path.parent / "vault"
    print(json.dumps(viz.export_obsidian(config, conn, out)))


def cmd_summarize(args):
    from . import summarizer
    config, conn = _ctx()
    if getattr(args, "stale", False):
        rels = summarizer.select_stale(conn, args.limit, args.prefix)
        seen = set(rels)
        rels += [r for r in summarizer.select_central_missing(conn, args.limit - len(rels), args.prefix)
                 if r not in seen]
    else:
        rels = summarizer.select_central_missing(conn, args.limit, args.prefix)
    print(json.dumps(summarizer.run(config, conn, rels, workers=args.workers)))


def cmd_embed(_args):
    from . import embeddings
    config, conn = _ctx()
    print(json.dumps(embeddings.build(config, conn)))


_SESSION_DIRECTIVE = (
    "## Cerebro is active for this project\n"
    "This repo has a Cerebro brain: a cached index of its structure, symbols, "
    "dependencies and summaries. Querying it costs far fewer tokens than "
    "re-reading files. RULES for this session:\n"
    "1. BEFORE exploring with grep/find/ls/Read — or spawning an Explore/"
    "general-purpose subagent to look around — call cerebro_search(query) to "
    "locate code and cerebro_get(path) to learn a file (summary + symbols + "
    "dependencies) without opening it.\n"
    "2. Open a file's full contents ONLY when cerebro_get reports no summary or "
    "flags it STALE, or when you need exact implementation detail.\n"
    "3. After you understand a file you had to read, call "
    "cerebro_record(path, <1-3 sentence English summary>) so the next session "
    "reuses it instead of re-reading.\n"
    "4. Record decisions/domain rules/gotchas with cerebro_note, and call "
    "cerebro_recall before re-deriving the 'why' behind the code.\n\n"
    "Current map:\n\n"
)


def cmd_session(_args):
    """Combined SessionStart context (git-sync + map + decisions) in ONE process.
    Uses `views` (not `server`) so it skips the ~230ms MCP-SDK import — this is the
    per-session hot path."""
    from . import gitsync, views
    config, conn = _ctx()
    gitsync.sync(config, conn)  # catch branch switch / pull / external edits
    text = views.map_text(conn, config.root)
    if not text or "Index is empty" in text:
        return
    out = _SESSION_DIRECTIVE + text
    decisions = views.recall_text(conn)
    if decisions and "No notes recorded" not in decisions:
        out += "\n\n## Decisions on record (from past sessions):\n" + decisions
    print(out)


def cmd_doc_audit(args):
    """Living docs: flag vault notes whose referenced code changed or vanished."""
    from . import docaudit
    _, conn = _ctx()
    vault = Path(args.vault).expanduser()
    aliases = dict(
        kv.split("=", 1) for kv in (args.aliases.split(",") if args.aliases else []) if "=" in kv
    )
    results = docaudit.audit_vault(conn, vault, aliases)
    stale = [r for r in results if r["status"] == "stale"]
    hints = [r for r in results if r["status"] == "hint"]
    if args.json:
        import json as _json
        print(_json.dumps(
            [{"note": str(r["note"]), "status": r["status"], "issues": r["issues"]} for r in results],
            indent=2, ensure_ascii=False))
        return
    fresh = len(results) - len(stale) - len(hints)
    print(f"Audited {len(results)} notes with code refs: "
          f"⚠ {len(stale)} stale · {len(hints)} hints · {fresh} fresh\n")
    for r in stale:
        print(f"⚠ STALE  {r['note'].name}")
        for kind, msg in r["issues"]:
            if kind in ("broken", "changed"):
                print(f"    [{kind}] {msg}")
        if args.fix and docaudit.mark_stale(r["note"]):
            print("    → frontmatter set to estado: revisar")
    if args.show_hints and hints:
        print("\nHints (heuristic — symbol names not found):")
        for r in hints[:20]:
            syms = ", ".join(m.split("`")[1] for k, m in r["issues"] if k == "symbol?")
            print(f"  {r['note'].name}: {syms}")


def cmd_doc_refresh(args):
    """Re-audit a single stale note against live code; print the refresh briefing."""
    from . import docaudit
    _, conn = _ctx()
    aliases = dict(
        kv.split("=", 1) for kv in (args.aliases.split(",") if args.aliases else []) if "=" in kv
    )
    briefing = docaudit.refresh_briefing(conn, Path(args.note).expanduser(), aliases)
    print(docaudit.format_briefing(briefing))


def cmd_setup(args):
    """One-command onboarding for the current repo."""
    from . import indexer, gitsync
    config, conn = _ctx()
    print(f"🧠 Cerebro setup — {config.root}\n")
    res = indexer.reindex(config, conn)
    print(f"  ✓ indexed {res['total_files']} files ({res['new']} new, {res['changed']} changed)")
    gitsync.sync(config, conn)
    print("  ✓ git baseline recorded")

    if args.summarize:
        from . import summarizer
        rels = summarizer.select_central_missing(conn, args.summarize)
        sr = summarizer.run(config, conn, rels)
        print(f"  ✓ summarized {sr['summarized']} central files (claude -p)")
    if args.embed:
        from . import embeddings
        er = embeddings.build(config, conn)
        msg = er.get("reason") or f"{er.get('embedded', 0)} files"
        print(f"  ✓ semantic index: {msg}")

    proj = _project_root()
    print("\nNext steps:")
    print("  1) Register the MCP server with Claude Code:")
    print(
        f"     claude mcp add cerebro -s user -e CEREBRO_ROOT='{config.root}' "
        f"-- uv --directory '{proj}' run cerebro"
    )
    print("     (or install globally:  uv tool install --from '%s' cerebro )" % proj)
    print("  2) Optional auto-use: enable the SessionStart/PostToolUse hooks in")
    print(f"     {proj}/plugin/hooks  (see README) and copy plugin/skills/cerebro to ~/.claude/skills/")
    print("  3) Reload your editor, open a chat, and the cerebro_* tools are available.")


_COMMANDS = {
    "serve": cmd_serve, "setup": cmd_setup, "session-context": cmd_session,
    "doc-audit": cmd_doc_audit, "doc-refresh": cmd_doc_refresh,
    "index": cmd_index, "search": cmd_search,
    "map": cmd_map, "graph": cmd_graph, "graph-all": cmd_graph_all,
    "obsidian": cmd_obsidian, "summarize": cmd_summarize,
    "embed": cmd_embed, "impact": cmd_impact, "cycles": cmd_cycles, "orphans": cmd_orphans,
    "dead-symbols": cmd_orphans_symbols,
    "callers": cmd_callers, "calls": cmd_calls, "recall": cmd_recall,
    "endpoints": cmd_endpoints,
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cerebro", description="Persistent code-knowledge brain")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server (stdio) — the default with no args")
    sub.add_parser("session-context", help="combined SessionStart context (used by the hook)")
    s = sub.add_parser("doc-audit", help="flag vault notes whose referenced code changed (living docs)")
    s.add_argument("vault", help="path to the markdown knowledge vault")
    s.add_argument("--aliases", help="map wiki app names to repo dirs: 'backend_app=fenix-store-backend,...'")
    s.add_argument("--fix", action="store_true", help="patch stale notes' frontmatter to estado: revisar")
    s.add_argument("--json", action="store_true")
    s.add_argument("--show-hints", action="store_true", help="also show heuristic symbol-name hints")
    s = sub.add_parser("doc-refresh", help="re-audit one note vs live code → refresh briefing")
    s.add_argument("note", help="path to the stale note")
    s.add_argument("--aliases", help="map wiki app names to repo dirs")
    s = sub.add_parser("setup", help="index this repo + print MCP registration")
    s.add_argument("--summarize", type=int, nargs="?", const=30, default=0,
                   help="also warm summaries for the top N central files")
    s.add_argument("--embed", action="store_true", help="also build the semantic index")
    s = sub.add_parser("index", help="build/refresh the index")
    s.add_argument("paths", nargs="*"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("search", help="hybrid semantic + keyword search")
    s.add_argument("query", nargs="+")
    s = sub.add_parser("map", help="project overview"); s.add_argument("--top", type=int, default=30)
    s = sub.add_parser("graph", help="write the interactive dependency-graph HTML")
    s.add_argument("--limit", type=int, default=400); s.add_argument("--prefix"); s.add_argument("-o", "--out")
    sub.add_parser("graph-all", help="regenerate every graph declared in .cerebro/graphs.toml")
    s = sub.add_parser("obsidian", help="export an Obsidian vault"); s.add_argument("-o", "--out")
    s = sub.add_parser("summarize", help="warm summaries via claude -p")
    s.add_argument("--limit", type=int, default=20); s.add_argument("--prefix"); s.add_argument("--workers", type=int, default=4)
    s.add_argument("--stale", action="store_true", help="also re-summarize stale summaries (edited files)")
    sub.add_parser("embed", help="build the semantic index (needs --extra semantic)")
    s = sub.add_parser("impact", help="transitive blast radius of a file"); s.add_argument("path")
    sub.add_parser("cycles", help="circular-import groups")
    s = sub.add_parser("orphans", help="dead-code candidates (file-level)"); s.add_argument("--prefix", default="")
    s = sub.add_parser("dead-symbols", help="unused-export candidates (symbol-level)")
    s.add_argument("--prefix", default="")
    s = sub.add_parser("callers", help="call sites of a symbol"); s.add_argument("name")
    s = sub.add_parser("calls", help="internal calls a file makes"); s.add_argument("path")
    s = sub.add_parser("endpoints", help="backend HTTP endpoints (NestJS routes)"); s.add_argument("query", nargs="*")
    s = sub.add_parser("recall", help="recall recorded decisions"); s.add_argument("query", nargs="*")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:  # no subcommand -> MCP server (keeps `... run cerebro` working)
        cmd_serve(None)
        return
    args = _build_parser().parse_args(argv)
    if not args.cmd:
        cmd_serve(None)
        return
    _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    main()
