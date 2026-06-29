from cerebro import db, indexer, summarizer, summaries


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_select_central_missing_skips_summarized(tmp_path, project):
    config, conn = project
    write(tmp_path, "util.py", "def helper():\n    return 1\n")
    write(tmp_path, "main.py", "from util import helper\n")
    indexer.reindex(config, conn)

    targets = summarizer.select_central_missing(conn, limit=10)
    assert "util.py" in targets  # central + no summary

    summaries.record(conn, "util.py", "helper utils")
    assert "util.py" not in summarizer.select_central_missing(conn, limit=10)


def test_select_respects_prefix(tmp_path, project):
    config, conn = project
    write(tmp_path, "pkg/a.py", "x = 1\n")
    write(tmp_path, "other/b.py", "y = 2\n")
    indexer.reindex(config, conn)
    targets = summarizer.select_central_missing(conn, limit=10, prefix="pkg/")
    assert all(t.startswith("pkg/") for t in targets)


def test_run_records_generated_summaries(tmp_path, project, monkeypatch):
    config, conn = project
    write(tmp_path, "a.py", "x = 1\n")
    indexer.reindex(config, conn)
    # don't actually call claude -p in tests
    monkeypatch.setattr(summarizer, "summarize_one", lambda c, rel, model: f"summary of {rel}")
    res = summarizer.run(config, conn, ["a.py"], workers=1)
    assert res["summarized"] == 1
    assert summaries.get(conn, "a.py")["summary_en"] == "summary of a.py"


def test_select_stale_finds_changed_summaries(tmp_path, project):
    """A summary whose source file changed (edit + reindex) surfaces as a stale
    candidate, so the SessionEnd auto-record can re-warm it."""
    config, conn = project
    write(tmp_path, "x.py", "def a():\n    return 1\n")
    indexer.reindex(config, conn)
    summaries.record(conn, "x.py", "does a")
    assert summarizer.select_stale(conn, 10) == []  # fresh — not stale

    write(tmp_path, "x.py", "def a():\n    return 2  # changed\n")
    indexer.reindex(config, conn)
    assert summarizer.select_stale(conn, 10) == ["x.py"]  # now stale


def test_select_stale_skips_non_source(tmp_path, project):
    config, conn = project
    write(tmp_path, "README.md", "# hi\n")
    indexer.reindex(config, conn)
    summaries.record(conn, "README.md", "readme")
    write(tmp_path, "README.md", "# hi changed\n")
    indexer.reindex(config, conn)
    assert summarizer.select_stale(conn, 10) == []  # .md has no language -> skipped
