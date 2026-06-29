"""Architecture insights derived from the dependency graph (no new analysis):

- impact(path): the transitive blast radius — everything that (directly or
  indirectly) imports `path`, so you know what a change can break.
- cycles(): circular-import groups (strongly connected components > 1), a smell.
- orphans(): code files nobody imports — dead-code candidates (minus the obvious
  framework entrypoints, which are loaded by convention, not by import).
"""
from __future__ import annotations

import json
from collections import deque

import networkx as nx

from . import graph as graphmod

_ENTRY_SUFFIXES = (
    ".module.ts", ".controller.ts", ".config.ts", ".config.js", ".config.mjs",
    ".config.cjs", ".d.ts", ".stories.tsx", ".stories.ts",
)
_ENTRY_BASENAMES = {
    "main.ts", "index.ts", "index.js", "index.tsx", "main.js",
    # Next.js root conventions: invoked by the framework, never imported.
    "middleware.ts", "middleware.js", "instrumentation.ts", "instrumentation.js",
    # Dart/Flutter: lib/main.dart is the app entry (runApp), invoked by the
    # runtime, never imported. Flavor variants (main_dev.dart, ...) match below.
    "main.dart",
}
_ENTRY_SEGMENTS = (
    "/pages/", "/app/", "/migrations/", "/seeders/", "/seeds/", "/scripts/",
    "/test/", "/tests/", "/__tests__/", "/e2e/",
    # Dart conventions: bin/ holds executables (each with a main()), and
    # integration_test/ is run by the Flutter test harness, not imported.
    "/bin/", "/integration_test/",
)


def looks_entrypoint(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if base in _ENTRY_BASENAMES or path.endswith(_ENTRY_SUFFIXES):
        return True
    # Dart flavor entrypoints (main_dev.dart, main_production.dart, ...).
    if base.startswith("main_") and base.endswith(".dart"):
        return True
    # JS uses foo.test.ts / foo.spec.ts; Dart uses foo_test.dart.
    if ".spec." in base or ".test." in base or ".e2e." in base or base.endswith("_test.dart"):
        return True
    return any(seg in ("/" + path) for seg in _ENTRY_SEGMENTS)


def impact(conn, path: str, limit: int = 300) -> dict | None:
    g = graphmod.build_graph(conn)
    if path not in g:
        return None
    rev = g.reverse(copy=False)  # edge v->u means "u imports v"
    dist = {}
    dq = deque([path])
    seen = {path}
    while dq:
        n = dq.popleft()
        for importer in rev.successors(n):
            if importer not in seen:
                seen.add(importer)
                dist[importer] = dist.get(n, 0) + 1
                dq.append(importer)
    by_dist: dict[int, int] = {}
    for d in dist.values():
        by_dist[d] = by_dist.get(d, 0) + 1
    items = sorted(dist.items(), key=lambda kv: (kv[1], kv[0]))
    direct = sorted(m for m, d in dist.items() if d == 1)
    return {
        "path": path,
        "total": len(dist),
        "direct": direct,
        "by_distance": dict(sorted(by_dist.items())),
        "all": [p for p, _ in items][:limit],
    }


def is_barrel(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base.startswith("index.") and base.rsplit(".", 1)[-1] in {
        "ts", "tsx", "js", "jsx", "mjs", "cjs"
    }


def cycles(conn, ignore_barrels: bool = True, max_report: int = 50) -> dict:
    """Circular-import groups. Barrel files (index.*) re-export everything and
    create huge artificial cycles, so by default they're removed first — what's
    left are genuine module cycles, reported tightest (shortest) first.

    Type-only edges (TS `import type`) are excluded: they're erased at compile
    time, so a cycle made purely of them never exists at runtime."""
    g = graphmod.build_graph(conn, exclude_kinds=("type",))
    barrels = 0
    if ignore_barrels:
        drop = [n for n in g.nodes if is_barrel(n)]
        barrels = len(drop)
        g = g.copy()
        g.remove_nodes_from(drop)
    out = []
    for comp in nx.strongly_connected_components(g):
        if len(comp) < 2:
            continue
        sub = g.subgraph(comp)
        try:
            cyc = nx.find_cycle(sub)
            chain = [u for u, _ in cyc] + [cyc[0][0]]
        except Exception:
            chain = sorted(comp)
        out.append(
            {"size": len(comp), "length": len(chain) - 1, "cycle": chain, "members": sorted(comp)}
        )
    out.sort(key=lambda c: (c["length"], c["size"]))  # tightest first
    return {"cycles": out[:max_report], "barrels_ignored": barrels, "total": len(out)}


def cycle_members(conn) -> set[str]:
    """Set of files participating in any (barrel-free) cycle — for graph overlays."""
    members: set[str] = set()
    for c in cycles(conn)["cycles"]:
        members.update(c["members"])
    return members


def script_entrypoints(conn) -> set[str]:
    """Files invoked by package.json scripts, recorded at index time. Loaded by
    tooling rather than imported, so they're entrypoints, not dead code."""
    row = conn.execute("SELECT value FROM meta WHERE key='script_entrypoints'").fetchone()
    if not row or not row["value"]:
        return set()
    try:
        return set(json.loads(row["value"]))
    except (ValueError, TypeError):
        return set()


# Methods invoked by a framework via interface/lifecycle contract, never by name
# in code — so "referenced nowhere" doesn't mean dead. Skipped in dead_symbols.
_FRAMEWORK_METHODS = {
    # NestJS
    "use", "canActivate", "intercept", "transform", "catch",
    "onModuleInit", "onModuleDestroy", "onApplicationBootstrap",
    "onApplicationShutdown", "beforeApplicationShutdown",
    # Angular
    "ngOnInit", "ngOnDestroy", "ngOnChanges", "ngAfterViewInit",
    "ngAfterViewChecked", "ngDoCheck", "ngAfterContentInit",
    # React class lifecycle
    "render", "componentDidMount", "componentWillUnmount", "componentDidUpdate",
    "shouldComponentUpdate", "getDerivedStateFromProps", "getSnapshotBeforeUpdate",
    # Flutter widget/state lifecycle — overridden, invoked by the framework
    "build", "createState", "initState", "dispose", "didChangeDependencies",
    "didUpdateWidget", "deactivate", "reassemble", "createElement",
    "didChangeAppLifecycleState",
    # flutter_bloc / Cubit contract overrides; Equatable's props
    "onChange", "onTransition", "onError", "close", "props",
}


def _project_of(path: str) -> str:
    """The independent repo a file belongs to — its top-level directory. Cerebro
    indexes a workspace of separate projects (each its own git repo) under one
    root, with zero import edges crossing between them."""
    return path.split("/", 1)[0]


def dead_symbols(conn, prefix: str | None = None, limit: int = 300) -> dict:
    """Top-level/exported symbols whose name is referenced nowhere *in their own
    project* — unused-export candidates inside otherwise-live files (the dead code
    orphans() can't see, since the file itself is imported).

    Matching is per-project, not global: the workspace holds independent repos
    (no cross-project import edges), so a same-named symbol in another project is
    unrelated and must NOT count as a use — global pooling would hide that dead
    code. Uses the `refs` table (name uses, minus definition sites). Heuristic,
    erring toward silence: same-named symbols within one project still mask each
    other, and dynamic access (obj['x'], string DI) can yield false positives — a
    lead to confirm, not a delete list. Framework entrypoints are skipped."""
    scripts = script_entrypoints(conn)
    langs = {r["path"]: r["lang"] for r in conn.execute("SELECT path, lang FROM files")}
    # name -> set of projects that reference it.
    ref_projects: dict[str, set[str]] = {}
    for r in conn.execute("SELECT name, path FROM refs"):
        ref_projects.setdefault(r["name"], set()).add(_project_of(r["path"]))
    out = []
    for r in conn.execute(
        "SELECT file_path AS path, kind, name, line FROM symbols ORDER BY file_path, line"
    ):
        path = r["path"]
        if langs.get(path) is None:
            continue
        if prefix and not path.startswith(prefix):
            continue
        if looks_entrypoint(path) or path in scripts:
            continue
        if r["kind"] == "method" and r["name"] in _FRAMEWORK_METHODS:
            continue  # invoked by the framework via contract, not by name
        if _project_of(path) in ref_projects.get(r["name"], ()):
            continue  # referenced somewhere in its own project -> alive
        out.append({"path": path, "name": r["name"], "kind": r["kind"], "line": r["line"]})
    return {"dead": out[:limit], "total": len(out)}


def orphans(conn, prefix: str | None = None) -> dict:
    g = graphmod.build_graph(conn)
    langs = {r["path"]: r["lang"] for r in conn.execute("SELECT path, lang FROM files")}
    scripts = script_entrypoints(conn)
    dead, entry = [], []
    for n in g.nodes:
        if langs.get(n) is None:
            continue
        if prefix and not n.startswith(prefix):
            continue
        if g.in_degree(n) == 0:
            (entry if (looks_entrypoint(n) or n in scripts) else dead).append(n)
    return {"dead": sorted(dead), "entrypoints": sorted(entry)}
