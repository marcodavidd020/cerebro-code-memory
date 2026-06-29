import json

from cerebro import indexer, summaries, viz


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _project_with_edges(tmp_path, project):
    config, conn = project
    write(tmp_path, "pkg/util.py", "def helper():\n    return 1\n")
    write(tmp_path, "pkg/main.py", "from pkg.util import helper\n\ndef run():\n    return helper()\n")
    indexer.reindex(config, conn)
    summaries.record(conn, "pkg/util.py", "Shared helper utilities.")
    return config, conn


def test_graph_html_embeds_nodes_and_edges(tmp_path, project):
    config, conn = _project_with_edges(tmp_path, project)
    htmls = viz.graph_html(conn)
    assert "force-graph" in htmls
    # the data payload contains our files and the dependency edge
    start = htmls.index("const DATA = ") + len("const DATA = ")
    data = json.loads(htmls[start : htmls.index(", META =", start)])
    ids = {n["id"] for n in data["nodes"]}
    assert {"pkg/util.py", "pkg/main.py"} <= ids
    assert {"from": "pkg/main.py", "to": "pkg/util.py"} in data["edges"]
    # summary travels into the node for the side panel
    util = next(n for n in data["nodes"] if n["id"] == "pkg/util.py")
    assert "helper utilities" in util["summary"]


def test_graph_includes_orphans_beyond_centrality_limit(tmp_path, project):
    config, conn = project
    write(tmp_path, "hub.py", "def h():\n    return 1\n")
    write(tmp_path, "a.py", "from hub import h\n")
    write(tmp_path, "b.py", "from hub import h\n")
    write(tmp_path, "lonely.py", "from hub import h\n")  # imports hub, imported by nobody
    indexer.reindex(config, conn)
    # limit=1 keeps only the most central node (hub); the orphan overlay must still
    # pull lonely.py in, otherwise the "Orphans" button highlights nothing.
    htmls = viz.graph_html(conn, limit=1)
    start = htmls.index("const DATA = ") + len("const DATA = ")
    data = json.loads(htmls[start : htmls.index(", META =", start)])
    nodes = {n["id"]: n for n in data["nodes"]}
    assert "lonely.py" in nodes
    assert nodes["lonely.py"]["orphan"] is True


def test_obsidian_export_writes_linked_notes(tmp_path, project):
    config, conn = _project_with_edges(tmp_path, project)
    out = tmp_path / "vault"
    res = viz.export_obsidian(config, conn, out)
    assert res["notes"] == 2
    main_note = (out / "pkg/main.py.md").read_text()
    assert "[[pkg/util.py]]" in main_note  # forward link -> Obsidian graph edge
    util_note = (out / "pkg/util.py.md").read_text()
    assert "Shared helper utilities." in util_note
    assert "tags: [pkg, python]" in util_note
