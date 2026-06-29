from cerebro import cli


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_cli_index_map_search(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CEREBRO_ROOT", str(tmp_path))
    write(tmp_path, "pkg/util.py", "def helper():\n    return 1\n")
    write(tmp_path, "pkg/main.py", "from pkg.util import helper\n\ndef run():\n    return helper()\n")

    cli.main(["index"])
    assert '"total_files": 2' in capsys.readouterr().out

    cli.main(["map", "--top", "5"])
    assert "Cerebro map" in capsys.readouterr().out

    cli.main(["search", "helper"])
    assert "util" in capsys.readouterr().out


def test_cli_setup_prints_registration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CEREBRO_ROOT", str(tmp_path))
    write(tmp_path, "a.py", "def a():\n    return 1\n")
    cli.main(["setup"])
    out = capsys.readouterr().out
    assert "indexed" in out
    assert "claude mcp add cerebro" in out
    assert str(tmp_path) in out  # the registration command carries the resolved root


def test_cli_callers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CEREBRO_ROOT", str(tmp_path))
    write(tmp_path, "util.py", "def helper():\n    return 1\n")
    write(tmp_path, "main.py", "from util import helper\n\ndef run():\n    return helper()\n")
    cli.main(["index"])
    capsys.readouterr()
    cli.main(["callers", "helper"])
    assert "run" in capsys.readouterr().out


def test_cli_graph_all(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CEREBRO_ROOT", str(tmp_path))
    write(tmp_path, "pkg/util.py", "def helper():\n    return 1\n")
    write(tmp_path, "pkg/main.py", "from pkg.util import helper\n\ndef run():\n    return helper()\n")
    cli.main(["index"])
    capsys.readouterr()
    cerebro_dir = tmp_path / ".cerebro"

    # No graphs.toml -> instant no-op, so the SessionStart hook is safe to run on
    # every project (it must not write graphs into repos that never opted in).
    cli.main(["graph-all"])
    assert '"skipped"' in capsys.readouterr().out
    assert not list(cerebro_dir.glob("*.html"))

    # With a config, each [[graph]] entry renders its HTML; the unnamed entry
    # writes the global cerebro-graph.html, a named one writes cerebro-graph-<name>.html.
    (cerebro_dir / "graphs.toml").write_text(
        "[[graph]]\nlimit = 50\n\n[[graph]]\nname = \"pkg\"\nprefix = \"pkg/\"\nlimit = 10\n"
    )
    cli.main(["graph-all"])
    assert '"written"' in capsys.readouterr().out
    assert (cerebro_dir / "cerebro-graph.html").exists()
    assert (cerebro_dir / "cerebro-graph-pkg.html").exists()
