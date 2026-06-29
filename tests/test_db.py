from cerebro import db, summaries


def test_schema_and_file_upsert(project):
    _, conn = project
    db.upsert_file(conn, "a.py", "python", "h1", 1.0, 10, "2026-01-01T00:00:00")
    assert db.stored_hashes(conn) == {"a.py": "h1"}
    # upsert is idempotent on path
    db.upsert_file(conn, "a.py", "python", "h2", 2.0, 20, "2026-01-02T00:00:00")
    assert db.stored_hashes(conn) == {"a.py": "h2"}


def test_symbols_and_fts(project):
    _, conn = project
    db.upsert_file(conn, "a.py", "python", "h1", 1.0, 10, "t")
    db.replace_symbols(
        conn,
        "a.py",
        [("function", "login_user", 3, "def login_user(creds):"), ("class", "Auth", 1, "class Auth:")],
    )
    conn.commit()
    rows = db.symbols_for(conn, "a.py")
    assert {r["name"] for r in rows} == {"login_user", "Auth"}
    # symbol names are searchable via FTS
    hits = db.search(conn, "login_user")
    assert any(r["path"] == "a.py" for r in hits)


def test_replace_symbols_is_not_additive(project):
    _, conn = project
    db.upsert_file(conn, "a.py", "python", "h1", 1.0, 10, "t")
    db.replace_symbols(conn, "a.py", [("function", "old", 1, "def old():")])
    db.replace_symbols(conn, "a.py", [("function", "new", 1, "def new():")])
    conn.commit()
    names = {r["name"] for r in db.symbols_for(conn, "a.py")}
    assert names == {"new"}


def test_summary_record_get_and_staleness(project):
    _, conn = project
    db.upsert_file(conn, "a.py", "python", "hash_v1", 1.0, 10, "t")
    summaries.record(conn, "a.py", "Handles authentication.")
    got = summaries.get(conn, "a.py")
    assert got["summary_en"] == "Handles authentication."
    assert got["stale"] is False
    # same content via explicit current_hash -> fresh
    assert summaries.get(conn, "a.py", current_hash="hash_v1")["stale"] is False
    # different live hash -> stale
    assert summaries.get(conn, "a.py", current_hash="hash_v2")["stale"] is True
    # summary is searchable
    assert any(r["path"] == "a.py" for r in db.search(conn, "authentication"))


def test_stale_summaries_after_index_change(project):
    _, conn = project
    db.upsert_file(conn, "a.py", "python", "v1", 1.0, 10, "t")
    summaries.record(conn, "a.py", "summary")
    assert summaries.stale_summaries(conn) == []
    # the indexed hash advances past the summary's source_hash
    db.upsert_file(conn, "a.py", "python", "v2", 2.0, 11, "t2")
    conn.commit()
    assert summaries.stale_summaries(conn) == ["a.py"]


def test_forget_file_removes_all_traces(project):
    _, conn = project
    db.upsert_file(conn, "a.py", "python", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "a.py", [("function", "f", 1, "def f():")])
    db.replace_edges(conn, "a.py", ["b.py"])
    summaries.record(conn, "a.py", "s")
    db.forget_file(conn, "a.py")
    conn.commit()
    assert db.stored_hashes(conn) == {}
    assert db.symbols_for(conn, "a.py") == []
    assert summaries.get(conn, "a.py") is None
    assert db.search(conn, "s") == []
