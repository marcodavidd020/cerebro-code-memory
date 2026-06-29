from cerebro import db, notes, server


def test_add_and_recall_recent(project):
    _, conn = project
    notes.add(conn, "payments", "QR_MANUAL means the merchant confirms payment by hand.")
    notes.add(conn, "auth", "JWT in checkout is the buyer's, not the merchant's.")
    recent = notes.recall(conn)  # no query -> most recent first
    assert recent[0]["topic"] == "auth"
    assert len(recent) == 2


def test_recall_by_query(project):
    _, conn = project
    notes.add(conn, "payments", "QR_MANUAL means the merchant confirms payment by hand.")
    notes.add(conn, "refactor", "Seller entity was renamed to Organization everywhere.")
    hits = notes.recall(conn, "organization")
    assert any("Organization" in r["content"] for r in hits)
    assert all("QR_MANUAL" not in r["content"] for r in hits)


def test_notes_excluded_from_code_search(project):
    _, conn = project
    db.upsert_file(conn, "a.py", "python", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "a.py", [("function", "login", 1, "def login():")])
    conn.commit()
    notes.add(conn, "auth", "login uses bcrypt")
    # code search must not return the note row
    results = db.search(conn, "login")
    kinds = {r["kind"] for r in results}
    assert "note" not in kinds
    assert any(r["path"] == "a.py" for r in results)


def test_recall_tool_roundtrip(project):
    config, conn = project
    server._CONFIG, server._CONN = config, conn
    out = server.cerebro_note("Stock is reserved with a pessimistic lock at checkout.", topic="stock")
    assert "Recorded note" in out
    recalled = server.cerebro_recall("stock")
    assert "pessimistic lock" in recalled
