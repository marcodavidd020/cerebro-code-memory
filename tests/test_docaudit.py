from datetime import datetime

from cerebro import db, docaudit


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_audit_stale_broken_fresh(tmp_path, project):
    _, conn = project
    jan = datetime(2026, 1, 1).timestamp()
    jun = datetime(2026, 6, 1).timestamp()
    db.upsert_file(conn, "src/a.ts", "typescript", "h", jan, 10, "t")  # unchanged since note
    db.upsert_file(conn, "src/b.ts", "typescript", "h", jun, 10, "t")  # changed after note
    db.replace_symbols(conn, "src/a.ts", [("function", "doThing", 1, "")])
    conn.commit()

    vault = tmp_path / "vault"
    write(vault / "fresh.md", "---\nultima_verificacion: 2026-03-01\n---\nUses `src/a.ts` and `doThing()`.")
    write(vault / "stale.md", "---\nultima_verificacion: 2026-03-01\n---\nUses `src/b.ts`.")
    write(vault / "broken.md", "---\nfecha: 2026-03-01\n---\nUses `src/missing.ts`.")

    res = {r["note"].name: r for r in docaudit.audit_vault(conn, vault)}
    assert res["fresh.md"]["status"] == "fresh"
    assert res["stale.md"]["status"] == "stale"
    assert any(k == "changed" for k, _ in res["stale.md"]["issues"])
    assert res["broken.md"]["status"] == "stale"
    assert any(k == "broken" for k, _ in res["broken.md"]["issues"])


def test_symbol_hint(tmp_path, project):
    _, conn = project
    db.upsert_file(conn, "src/a.ts", "typescript", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "src/a.ts", [("function", "realFn", 1, "")])
    conn.commit()
    vault = tmp_path / "v"
    write(vault / "n.md", "It refers to `ghostMethod()` which was removed.")
    res = {r["note"].name: r for r in docaudit.audit_vault(conn, vault)}
    assert res["n.md"]["status"] == "hint"
    assert any("ghostMethod" in m for k, m in res["n.md"]["issues"])


def test_mark_stale_patches_frontmatter(tmp_path):
    p = write(tmp_path / "n.md", "---\ntipo: conocimiento\nestado: vigente\n---\n# x")
    assert docaudit.mark_stale(p)
    text = p.read_text()
    assert "estado: revisar" in text and "estado: vigente" not in text


def test_refresh_briefing_gives_current_facts_and_relocation(tmp_path, project):
    _, conn = project
    jun = datetime(2026, 6, 1).timestamp()
    db.upsert_file(conn, "src/svc/order.service.ts", "typescript", "h", jun, 10, "t")
    db.replace_symbols(conn, "src/svc/order.service.ts", [("method", "checkout", 58, "async checkout(")])
    conn.commit()
    vault = tmp_path / "v"
    write(
        vault / "orders.md",
        "---\nultima_verificacion: 2026-03-01\n---\n"
        "Uses `src/svc/order.service.ts` and the old `src/legacy/order.service.ts`.",
    )
    b = docaudit.refresh_briefing(conn, vault / "orders.md")
    current = next(r for r in b["refs"] if r.get("resolved") == "src/svc/order.service.ts")
    assert current["status"] == "changed"
    assert any("checkout" in s for s in current["symbols"])
    moved = next(r for r in b["refs"] if r["status"] == "moved/missing")
    assert "src/svc/order.service.ts" in moved["candidates"]  # relocated by basename


def test_conceptual_note_skipped(tmp_path, project):
    _, conn = project
    vault = tmp_path / "v"
    write(vault / "concept.md", "---\ntipo: conocimiento\n---\nPure prose, no code references at all.")
    assert docaudit.audit_vault(conn, vault) == []  # nothing to verify
