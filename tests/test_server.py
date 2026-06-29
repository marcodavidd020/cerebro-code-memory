from cerebro import server, indexer, db, summaries


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_record_absolute_then_get_relative(tmp_path, project):
    """Regression for the real production bug: a summary recorded under an
    absolute path was invisible to a later cerebro_get using a relative one."""
    config, conn = project
    server._CONFIG, server._CONN = config, conn
    write(tmp_path, "pkg/svc.py", "def run():\n    return 1\n")
    indexer.reindex(config, conn)

    out = server.cerebro_record(str(tmp_path / "pkg/svc.py"), "Service entry point.")
    assert "not in the index" not in out  # absolute path resolved to indexed key

    got = server.cerebro_get("pkg/svc.py")  # different form, same file
    assert "Service entry point." in got
    assert "none yet" not in got


def test_resolve_path_forms(tmp_path, project):
    config, conn = project
    server._CONFIG, server._CONN = config, conn
    write(tmp_path, "a/b/c.py", "x = 1\n")
    indexer.reindex(config, conn)

    assert server._resolve_path(config, conn, str(tmp_path / "a/b/c.py")) == "a/b/c.py"
    assert server._resolve_path(config, conn, "./a/b/c.py") == "a/b/c.py"
    assert server._resolve_path(config, conn, "a/b/c.py") == "a/b/c.py"
    assert server._resolve_path(config, conn, "c.py") == "a/b/c.py"  # unique suffix


def test_cerebro_get_caps_long_dependent_list(tmp_path, project):
    config, conn = project
    server._CONFIG, server._CONN = config, conn
    write(tmp_path, "util.py", "def helper():\n    return 1\n")
    for i in range(20):
        write(tmp_path, f"m{i}.py", "from util import helper\n")
    indexer.reindex(config, conn)
    out = server.cerebro_get("util.py")
    assert "more)" in out  # 20 importers > cap -> collapsed


def test_search_ranks_summaries_before_symbols(project):
    config, conn = project
    server._CONFIG, server._CONN = config, conn
    db.upsert_file(conn, "svc.py", "python", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "svc.py", [("function", "checkout", 1, "def checkout():")])
    db.upsert_file(conn, "doc.py", "python", "h", 1.0, 10, "t")
    summaries.record(conn, "doc.py", "Handles the checkout flow end to end.")
    conn.commit()
    rows = db.search(conn, "checkout")
    assert rows[0]["kind"] == "summary"
