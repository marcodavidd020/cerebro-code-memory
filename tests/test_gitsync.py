import subprocess

from cerebro import db, gitsync, indexer


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def init_repo(root):
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")


def test_sync_reindexes_committed_changes(tmp_path, project):
    config, conn = project
    init_repo(tmp_path)
    write(tmp_path, "a.py", "def a():\n    return 1\n")
    git(tmp_path, "add", "a.py")
    git(tmp_path, "commit", "-q", "-m", "init")
    indexer.reindex(config, conn)

    assert gitsync.sync(config, conn)["git"] is True  # baseline HEAD

    # change + commit OUTSIDE cerebro (simulating a pull / external edit)
    write(tmp_path, "a.py", "def a():\n    return 1\n\ndef a2():\n    return 2\n")
    git(tmp_path, "add", "a.py")
    git(tmp_path, "commit", "-q", "-m", "change")

    result = gitsync.sync(config, conn)
    assert result["changed"] >= 1
    assert {r["name"] for r in db.symbols_for(conn, "a.py")} == {"a", "a2"}


def test_find_git_repos_nested(tmp_path, project):
    config, _ = project
    for app in ("app1", "app2"):
        (tmp_path / app).mkdir()
        init_repo(tmp_path / app)
    assert set(gitsync.find_git_repos(config)) == {"app1", "app2"}


def test_sync_no_git_is_noop(tmp_path, project):
    config, conn = project
    assert gitsync.sync(config, conn) == {"git": False, "changed": 0}
