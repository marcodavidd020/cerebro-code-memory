"""Dependency-graph analysis over the `edges` table.

An edge src -> dst means "src imports dst". PageRank therefore scores
widely-imported files (shared utilities, core modules) highest, which is what we
want to surface first in the map. NOT shortest-path / Dijkstra — code knowledge
is a relevance problem, not a routing one.
"""
from __future__ import annotations

import networkx as nx

from . import db


def build_graph(conn, exclude_kinds: tuple[str, ...] = ()) -> nx.DiGraph:
    """Build the import digraph. Pass exclude_kinds=('type',) to drop edges that
    are elided at runtime (TS type-only imports) — e.g. for cycle detection, where
    a type-only cycle is cosmetic, not a real startup-order problem."""
    g = nx.DiGraph()
    for row in conn.execute("SELECT path FROM files"):
        g.add_node(row["path"])
    if exclude_kinds:
        ph = ",".join("?" * len(exclude_kinds))
        rows = conn.execute(
            f"SELECT src_path, dst_path FROM edges WHERE kind NOT IN ({ph})", exclude_kinds
        )
    else:
        rows = conn.execute("SELECT src_path, dst_path FROM edges")
    for row in rows:
        g.add_edge(row["src_path"], row["dst_path"])
    return g


def _pagerank(g, damping: float = 0.85, iters: int = 50, tol: float = 1e-6):
    """Pure-Python power-iteration PageRank.

    Kept dependency-light on purpose: networkx's own pagerank requires scipy,
    which would be a heavy add for ~15 lines of standard math. Dangling nodes
    (no outgoing edges) redistribute their rank uniformly.
    """
    nodes = list(g.nodes)
    n = len(nodes)
    if n == 0:
        return {}
    out_deg = {v: g.out_degree(v) for v in nodes}
    rank = {v: 1.0 / n for v in nodes}
    for _ in range(iters):
        dangling = sum(rank[v] for v in nodes if out_deg[v] == 0)
        nxt = {}
        for v in nodes:
            inflow = sum(rank[u] / out_deg[u] for u in g.predecessors(v) if out_deg[u])
            nxt[v] = (1 - damping) / n + damping * (inflow + dangling / n)
        delta = sum(abs(nxt[v] - rank[v]) for v in nodes)
        rank = nxt
        if delta < tol:
            break
    return rank


def rank(conn, top: int | None = None):
    """Return [(path, score), ...] ordered by importance (descending)."""
    g = build_graph(conn)
    scores = _pagerank(g)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:top] if top else ranked


def dependents(conn, path: str) -> list[str]:
    """Files that import `path` (impact analysis: change `path`, these may break)."""
    g = build_graph(conn)
    return sorted(g.predecessors(path)) if path in g else []


def dependencies(conn, path: str) -> list[str]:
    """Files that `path` imports."""
    g = build_graph(conn)
    return sorted(g.successors(path)) if path in g else []
