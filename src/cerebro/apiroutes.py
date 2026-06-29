"""Backend HTTP endpoint extraction — slice 1 of API-call tracing (front↔back).

Cerebro's import/call graphs miss the network boundary: a storefront calling a
backend endpoint over HTTP is a real dependency invisible to `import` edges. This
extracts the BACKEND side — the routes a server exposes — so a session can answer
"where is POST /carts/lines handled?" cheaply, and so a later slice can match
frontend HTTP calls to these routes (the actual HTTP_CALLS edges).

v1 targets NestJS controllers (the Fenix backend): `@Controller('base')` plus the
method decorators `@Get/@Post/@Put/@Patch/@Delete/@All`. Best-effort line scan —
robust for the conventional one-controller-per-file layout. Read-only; computed on
demand from `*.controller.ts` files (a small subset), so no schema change.
"""
from __future__ import annotations

import re

_CONTROLLER = re.compile(r"@Controller\(\s*['\"`]([^'\"`]*)['\"`]")
_CONTROLLER_BARE = re.compile(r"@Controller\(\s*\)")
_METHOD = re.compile(r"@(Get|Post|Put|Patch|Delete|All)\(\s*(?:['\"`]([^'\"`]*)['\"`])?")
# A method declaration line: optional modifiers then `name(`. Used to attach a
# handler name to the decorator above it.
_HANDLER = re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|async\s+|static\s+)*"
                      r"([A-Za-z_]\w*)\s*\(")

_HTTP = ("GET", "POST", "PUT", "PATCH", "DELETE", "ALL")


def _join(base: str | None, path: str | None) -> str:
    parts = [p.strip("/") for p in (base or "", path or "") if p and p.strip("/")]
    return "/" + "/".join(parts)


def extract_file(rel: str, text: str) -> list[dict]:
    """Endpoints declared in one controller source. Each: method, path, file,
    line, handler."""
    base: str | None = None
    out: list[dict] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        mc = _CONTROLLER.search(line)
        if mc:
            base = mc.group(1)
            continue
        if _CONTROLLER_BARE.search(line):
            base = ""
            continue
        mm = _METHOD.search(line)
        if not mm:
            continue
        method = mm.group(1).upper()
        path = mm.group(2) or ""
        handler = None
        for j in range(i + 1, min(i + 6, len(lines))):
            stripped = lines[j].lstrip()
            if stripped.startswith("@"):
                continue  # another decorator (e.g. @UseGuards) — keep looking
            mh = _HANDLER.match(lines[j])
            if mh:
                handler = mh.group(1)
            break
        out.append({"method": method, "path": _join(base, path),
                    "file": rel, "line": i + 1, "handler": handler})
    return out


def find(config, conn, query: str = "") -> list[dict]:
    """All backend endpoints (optionally filtered by `query`), across the indexed
    NestJS controllers. Sorted by path then method."""
    rows = conn.execute(
        "SELECT path FROM files WHERE path LIKE '%.controller.ts' ORDER BY path"
    ).fetchall()
    eps: list[dict] = []
    for r in rows:
        rel = r["path"]
        try:
            text = (config.root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        eps.extend(extract_file(rel, text))
    if query:
        q = query.lower()
        eps = [
            e for e in eps
            if q in f"{e['method']} {e['path']} {e['handler'] or ''} {e['file']}".lower()
        ]
    eps.sort(key=lambda e: (e["path"], e["method"]))
    return eps
