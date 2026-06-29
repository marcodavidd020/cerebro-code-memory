from cerebro import callgraph, indexer


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_calls_extracted_with_enclosing_function(tmp_path, project):
    config, conn = project
    write(tmp_path, "util.py", "def helper():\n    return 1\n")
    write(
        tmp_path,
        "main.py",
        "from util import helper\n\n"
        "def run():\n    return helper()\n\n"
        "def go():\n    return run()\n",
    )
    indexer.reindex(config, conn)

    # who calls helper -> run() in main.py
    r = callgraph.callers(conn, "helper")
    assert "util.py" in r["defined_in"]
    assert ("main.py", "run") in {(s[0], s[1]) for s in r["sites"]}

    # who calls run -> go()
    r2 = callgraph.callers(conn, "run")
    assert "go" in {s[1] for s in r2["sites"]}


def test_calls_from_lists_internal_only(tmp_path, project):
    config, conn = project
    write(tmp_path, "util.py", "def helper():\n    return 1\n")
    write(
        tmp_path,
        "main.py",
        "from util import helper\n\n"
        "def run():\n    print(helper())\n    return helper()\n",
    )
    indexer.reindex(config, conn)
    cf = callgraph.calls_from(conn, "main.py")
    names = {dst for _, dst, _ in cf["calls"]}
    assert "helper" in names      # defined in repo -> kept
    assert "print" not in names   # builtin / not a defined symbol -> dropped


def test_calls_cleared_on_reindex(tmp_path, project):
    config, conn = project
    write(tmp_path, "util.py", "def helper():\n    return 1\n")
    write(tmp_path, "main.py", "from util import helper\n\ndef run():\n    return helper()\n")
    indexer.reindex(config, conn)
    assert callgraph.callers(conn, "helper")["count"] == 1
    # remove the call; reindex must drop the stale call edge
    write(tmp_path, "main.py", "def run():\n    return 0\n")
    indexer.reindex_paths(config, conn, ["main.py"])
    assert callgraph.callers(conn, "helper")["count"] == 0
