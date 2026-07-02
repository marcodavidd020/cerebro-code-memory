"""Symbol-aware staleness (#2): a summary is stale only when the file's STRUCTURE
(symbol signatures + imports) changes — not on comments, whitespace, or function
bodies. The byte source_hash is retained as a fallback for pre-migration brains.
"""
import sqlite3

from cerebro import db, indexer, summaries


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def reindex_summary(project, rel, first_src, summary, second_src):
    """Index `first_src`, record `summary`, then change to `second_src` and reindex.
    Returns (config, conn) ready to assert staleness."""
    config, conn = project
    write(config.root, rel, first_src)
    indexer.reindex(config, conn)
    summaries.record(conn, rel, summary)
    assert summaries.get(conn, rel)["stale"] is False  # fresh right after recording
    write(config.root, rel, second_src)
    indexer.reindex(config, conn)
    return config, conn


def test_comment_and_whitespace_change_is_not_stale(project):
    _, conn = reindex_summary(
        project, "m.py",
        "def a():\n    return 1\n",
        "does a thing",
        "# a new comment\ndef a():\n    return 1\n\n\n",
    )
    assert summaries.get(conn, "m.py")["stale"] is False
    assert summaries.stale_summaries(conn) == []


def test_body_change_same_signature_is_not_stale(project):
    _, conn = reindex_summary(
        project, "m.py",
        "def a():\n    return 1\n",
        "does a thing",
        "def a():\n    x = 2\n    return x + 40\n",  # body rewritten, signature identical
    )
    assert summaries.get(conn, "m.py")["stale"] is False
    assert summaries.stale_summaries(conn) == []


def test_signature_change_is_stale(project):
    _, conn = reindex_summary(
        project, "m.py",
        "def a():\n    return 1\n",
        "does a thing",
        "def a(x):\n    return x\n",  # signature changed
    )
    assert summaries.get(conn, "m.py")["stale"] is True
    assert summaries.stale_summaries(conn) == ["m.py"]


def test_new_import_is_stale(project):
    config, conn = project
    write(config.root, "dep.py", "def helper():\n    return 1\n")
    _, conn = reindex_summary(
        project, "m.py",
        "def a():\n    return 1\n",
        "does a thing",
        "import dep\ndef a():\n    return 1\n",  # same symbols, new dependency
    )
    assert summaries.get(conn, "m.py")["stale"] is True


def test_reordering_symbols_is_not_stale(project):
    _, conn = reindex_summary(
        project, "m.py",
        "def a():\n    return 1\n\ndef b():\n    return 2\n",
        "has a and b",
        "def b():\n    return 2\n\ndef a():\n    return 1\n",  # swapped order
    )
    assert summaries.get(conn, "m.py")["stale"] is False


def test_legacy_null_struct_hash_falls_back_to_bytes(project):
    config, conn = project
    write(config.root, "m.py", "def a():\n    return 1\n")
    indexer.reindex(config, conn)
    summaries.record(conn, "m.py", "does a thing")
    # Simulate a brain written before struct_hash existed: NULL on both sides.
    conn.execute("UPDATE summaries SET struct_hash=NULL WHERE path='m.py'")
    conn.execute("UPDATE files SET struct_hash=NULL WHERE path='m.py'")
    conn.commit()
    assert summaries.get(conn, "m.py")["stale"] is False        # bytes still match
    conn.execute("UPDATE files SET hash='deadbeef' WHERE path='m.py'")  # bytes diverge
    conn.commit()
    assert summaries.get(conn, "m.py")["stale"] is True          # byte fallback fires
    assert summaries.stale_summaries(conn) == ["m.py"]


def test_ensure_columns_migrates_pre_struct_hash_brain(tmp_path):
    """Opening an old brain (no struct_hash columns) ALTERs them in without losing
    rows — CREATE TABLE IF NOT EXISTS alone would never add the column."""
    dbp = tmp_path / ".cerebro" / "brain.db"
    dbp.parent.mkdir(parents=True)
    raw = sqlite3.connect(str(dbp))
    raw.execute("CREATE TABLE files (path TEXT PRIMARY KEY, lang TEXT, hash TEXT NOT NULL, "
                "mtime REAL, size INTEGER, indexed_at TEXT NOT NULL)")
    raw.execute("CREATE TABLE summaries (path TEXT PRIMARY KEY, summary_en TEXT NOT NULL, "
                "model TEXT, source_hash TEXT, updated_at TEXT NOT NULL)")
    raw.execute("INSERT INTO files(path, hash, indexed_at) VALUES('old.py','h1','t')")
    raw.execute("INSERT INTO summaries(path, summary_en, source_hash, updated_at) "
                "VALUES('old.py','old summary','h1','t')")
    raw.commit()
    raw.close()

    conn = db.connect(dbp)
    files_cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    sum_cols = {r["name"] for r in conn.execute("PRAGMA table_info(summaries)")}
    assert "struct_hash" in files_cols and "struct_hash" in sum_cols
    # Old row survives, struct_hash NULL, staleness falls back to bytes (fresh here).
    s = summaries.get(conn, "old.py")
    assert s is not None and s["stale"] is False
