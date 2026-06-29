"""Living-docs audit: cross-check a knowledge vault (Markdown notes) against the
Cerebro code index to find notes whose referenced code has changed or vanished.

This automates the rule every wiki states but no wiki enforces — "if the doc
contradicts the code, the code wins, mark the doc stale". A note is flagged when:
  - a code path it references no longer exists in the index (broken), or
  - a referenced file changed after the note's `ultima_verificacion` / `fecha`, or
  - a referenced symbol (method/class) is no longer defined anywhere (heuristic).
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import db as _db
from . import graph as _graph

_EXT = r"(?:ts|tsx|js|jsx|mjs|cjs|py)"
# a real code path: at least one '/', ending in a code extension, optional :line
_PATH_RE = re.compile(r"([\w.@-]+(?:/[\w.@-]+)+\." + _EXT + r")(?::(\d+))?")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.S)
_DATE_RE = re.compile(r"(?:ultima_verificacion|fecha)\s*:\s*(\d{4}-\d{2}-\d{2})")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def parse_note(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    fm = _FM_RE.match(text)
    fm_text = fm.group(1) if fm else ""
    dm = _DATE_RE.search(fm_text)
    files = {(m.group(1), m.group(2)) for m in _PATH_RE.finditer(text)}
    symbols = set()
    for m in _BACKTICK_RE.finditer(text):
        content = m.group(1)
        for call in re.findall(r"\b([A-Za-z_]\w{2,})\s*\(", content):  # foo(...)
            symbols.add(call)
        for tok in re.findall(r"\b([A-Za-z_]\w{2,})\b", content):  # mixedCase ident
            if re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok):
                symbols.add(tok)
    return {"date": dm.group(1) if dm else None, "files": files, "symbols": symbols}


def _normalize_ref(ref: str, aliases: dict[str, str]) -> str:
    """Resolve wiki naming to real repo paths: map logical app aliases
    (backend_app -> fenix-store-backend) and strip ../ / cross-machine prefixes."""
    parts = [p for p in ref.split("/") if p not in ("", ".")]
    for i, p in enumerate(parts):  # cut at the first known app-alias segment
        if p in aliases:
            parts = [aliases[p]] + parts[i + 1:]
            return "/".join(parts)
    while parts and parts[0] == "..":
        parts.pop(0)
    return "/".join(parts)


def _resolve(conn, ref: str, aliases: dict[str, str]) -> str | None:
    ref = _normalize_ref(ref, aliases)
    if conn.execute("SELECT 1 FROM files WHERE path=?", (ref,)).fetchone():
        return ref
    rows = conn.execute("SELECT path FROM files WHERE path LIKE ?", ("%/" + ref,)).fetchall()
    return rows[0]["path"] if len(rows) == 1 else None


def _epoch(date: str) -> float:
    return datetime.strptime(date, "%Y-%m-%d").timestamp()


def audit_note(conn, note: dict, known_symbols: set[str], aliases: dict[str, str]) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    note_epoch = _epoch(note["date"]) if note["date"] else None
    for ref, _line in sorted(note["files"]):
        resolved = _resolve(conn, ref, aliases)
        if resolved is None:
            issues.append(("broken", f"{ref} — not in the index"))
            continue
        if note_epoch is not None:
            row = conn.execute("SELECT mtime FROM files WHERE path=?", (resolved,)).fetchone()
            if row and row["mtime"] and row["mtime"] > note_epoch:
                mod = datetime.fromtimestamp(row["mtime"]).date().isoformat()
                issues.append(("changed", f"{resolved} changed {mod} (note verified {note['date']})"))
    for s in sorted(note["symbols"]):
        if s not in known_symbols:
            issues.append(("symbol?", f"`{s}` — not defined anywhere (renamed/removed?)"))
    return issues


def audit_vault(conn, vault: Path, aliases: dict[str, str] | None = None) -> list[dict]:
    aliases = aliases or {}
    known = {r["name"] for r in conn.execute("SELECT DISTINCT name FROM symbols")}
    results = []
    for md in sorted(vault.rglob("*.md")):
        if any(part.startswith(".") for part in md.parts):  # skip .obsidian etc.
            continue
        note = parse_note(md)
        if not note["files"] and not note["symbols"]:
            continue  # purely conceptual note — nothing to verify against code
        issues = audit_note(conn, note, known, aliases)
        hard = [i for i in issues if i[0] in ("broken", "changed")]
        status = "stale" if hard else ("hint" if issues else "fresh")
        results.append({"note": md, "status": status, "date": note["date"], "issues": issues})
    return results


def relocate(conn, ref: str) -> list[str]:
    """Where a moved/renamed file likely lives now — same basename in the index."""
    base = ref.split("/")[-1]
    return [r["path"] for r in conn.execute(
        "SELECT path FROM files WHERE path LIKE ?", ("%/" + base,)).fetchall()]


def refresh_briefing(conn, note_path: Path, aliases: dict[str, str] | None = None) -> dict:
    """Re-audit a stale note against the LIVE code: for each reference, the current
    facts (symbols, summary, dependents, last-change) or a relocation candidate if
    moved. This is the structured context an agent uses to propose the update."""
    aliases = aliases or {}
    note = parse_note(Path(note_path))
    note_epoch = _epoch(note["date"]) if note["date"] else None
    refs = []
    for ref, line in sorted(note["files"]):
        resolved = _resolve(conn, ref, aliases)
        if resolved is None:
            refs.append({"ref": ref, "status": "moved/missing", "candidates": relocate(conn, ref)})
            continue
        row = conn.execute("SELECT mtime FROM files WHERE path=?", (resolved,)).fetchone()
        changed = bool(note_epoch and row and row["mtime"] and row["mtime"] > note_epoch)
        srow = conn.execute("SELECT summary_en FROM summaries WHERE path=?", (resolved,)).fetchone()
        refs.append({
            "ref": ref, "resolved": resolved,
            "status": "changed" if changed else "current",
            "changed_date": datetime.fromtimestamp(row["mtime"]).date().isoformat() if row and row["mtime"] else None,
            "symbols": [f"L{s['line']} {s['kind']} {s['signature'] or s['name']}" for s in _db.symbols_for(conn, resolved)],
            "summary": srow["summary_en"] if srow else None,
            "dependents": _graph.dependents(conn, resolved)[:8],
        })
    return {"note": str(note_path), "date": note["date"], "refs": refs}


def format_briefing(b: dict) -> str:
    out = [f"# Refresh briefing — {Path(b['note']).name}  (verified {b['date'] or 'n/a'})", ""]
    for r in b["refs"]:
        if r["status"] == "moved/missing":
            cand = ", ".join(r["candidates"]) or "no candidate found"
            out.append(f"## ⚠ MOVED/MISSING: {r['ref']}\n   likely now → {cand}\n")
            continue
        tag = "CHANGED since note" if r["status"] == "changed" else "current"
        out.append(f"## {r['resolved']}  [{tag}, last change {r['changed_date']}]")
        if r["summary"]:
            out.append(f"   summary: {r['summary']}")
        if r["symbols"]:
            out.append("   symbols now:")
            out += [f"     {s}" for s in r["symbols"][:25]]
        if r["dependents"]:
            out.append("   used by: " + ", ".join(r["dependents"]))
        out.append("")
    return "\n".join(out)


_ESTADO_RE = re.compile(r"^(estado|status)\s*:.*$", re.M)


def mark_stale(path: Path) -> bool:
    """Patch the note's frontmatter to estado: revisar (their convention)."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    fm = _FM_RE.match(text)
    if not fm:
        return False
    block = fm.group(1)
    if _ESTADO_RE.search(block):
        new_block = _ESTADO_RE.sub("estado: revisar", block, count=1)
    else:
        new_block = block + "\nestado: revisar"
    path.write_text(text[: fm.start(1)] + new_block + text[fm.end(1):], encoding="utf-8")
    return True
