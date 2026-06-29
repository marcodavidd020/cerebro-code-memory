import json

from cerebro import db, insights


def _graph(conn):
    for p in ["a.py", "b.py", "c.py", "d.py", "util.py", "index.ts"]:
        lang = "python" if p.endswith(".py") else "typescript"
        db.upsert_file(conn, p, lang, "h", 1.0, 10, "t")
    db.replace_edges(conn, "c.py", ["a.py", "util.py"])  # c imports a and util
    db.replace_edges(conn, "a.py", ["b.py"])             # a <-> b cycle
    db.replace_edges(conn, "b.py", ["a.py"])
    conn.commit()


def test_impact_is_transitive(project):
    _, conn = project
    _graph(conn)
    r = insights.impact(conn, "util.py")
    assert r["total"] == 1 and r["direct"] == ["c.py"]  # only c imports util
    r2 = insights.impact(conn, "a.py")
    assert set(r2["direct"]) == {"b.py", "c.py"}          # both import a directly
    assert "c.py" in r2["all"]


def test_cycles_detected(project):
    _, conn = project
    _graph(conn)
    r = insights.cycles(conn)
    assert any(set(c["members"]) == {"a.py", "b.py"} for c in r["cycles"])


def test_cycles_ignore_barrels(project):
    _, conn = project
    # a -> index, index -> b, b -> a : a barrel-driven cycle that vanishes w/o index
    for p in ["a.py", "b.py", "index.ts"]:
        db.upsert_file(conn, p, "python" if p.endswith(".py") else "typescript", "h", 1.0, 10, "t")
    db.replace_edges(conn, "a.py", ["index.ts"])
    db.replace_edges(conn, "index.ts", ["b.py"])
    db.replace_edges(conn, "b.py", ["a.py"])
    conn.commit()
    assert insights.cycles(conn, ignore_barrels=True)["cycles"] == []   # barrel removed -> no cycle
    assert insights.cycles(conn, ignore_barrels=False)["total"] >= 1     # raw graph has the cycle


def test_orphans_split_dead_vs_entrypoint(project):
    _, conn = project
    _graph(conn)
    r = insights.orphans(conn)
    assert "d.py" in r["dead"]              # imported by nobody, not an entrypoint
    assert "c.py" in r["dead"]              # top-level consumer nothing imports
    assert "util.py" not in r["dead"]       # imported by c
    assert "index.ts" in r["entrypoints"]   # convention-loaded, excluded from dead


def test_dead_symbols_finds_unused_exports(project):
    _, conn = project
    for p in ["proj/lib.ts", "proj/app.ts"]:
        db.upsert_file(conn, p, "typescript", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "proj/lib.ts", [
        ("function", "used", 1, "used()"),
        ("function", "unused", 2, "unused()"),
    ])
    db.replace_refs(conn, "proj/app.ts", ["used"])  # same project references only `used`
    conn.commit()
    r = insights.dead_symbols(conn)
    dead = {d["name"] for d in r["dead"]}
    assert "unused" in dead          # referenced nowhere -> candidate
    assert "used" not in dead         # referenced within its project -> alive


def test_dead_symbols_skips_framework_methods(project):
    _, conn = project
    db.upsert_file(conn, "api/auth.middleware.ts", "typescript", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "api/auth.middleware.ts", [
        ("method", "use", 5, "use(req, res, next)"),      # NestJS invokes by contract
        ("method", "deadHelper", 9, "deadHelper()"),       # genuinely unused
    ])
    conn.commit()
    dead = {d["name"] for d in insights.dead_symbols(conn)["dead"]}
    assert "use" not in dead          # framework-invoked method, not flagged
    assert "deadHelper" in dead       # ordinary unused method still flagged


def test_dead_symbols_are_scoped_per_project(project):
    _, conn = project
    # `helper` is defined in projectA and (coincidentally) referenced only in
    # projectB — a different repo, so that reference must NOT keep it alive.
    db.upsert_file(conn, "projectA/lib.ts", "typescript", "h", 1.0, 10, "t")
    db.upsert_file(conn, "projectB/other.ts", "typescript", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "projectA/lib.ts", [("function", "helper", 1, "helper()")])
    db.replace_refs(conn, "projectB/other.ts", ["helper"])  # unrelated same-named use
    conn.commit()
    dead = {(d["path"], d["name"]) for d in insights.dead_symbols(conn)["dead"]}
    assert ("projectA/lib.ts", "helper") in dead  # cross-project name match ≠ use


def test_cycles_ignore_type_only_edges(project):
    _, conn = project
    for p in ["x.ts", "y.ts"]:
        db.upsert_file(conn, p, "typescript", "h", 1.0, 10, "t")
    # a cycle made purely of `import type` edges is erased at compile time
    db.replace_edges(conn, "x.ts", {"y.ts": "type"})
    db.replace_edges(conn, "y.ts", {"x.ts": "type"})
    conn.commit()
    assert insights.cycles(conn)["cycles"] == []   # type-only cycle is not real

    # only when BOTH edges are runtime is it a genuine startup-order cycle
    db.replace_edges(conn, "x.ts", {"y.ts": "import"})
    db.replace_edges(conn, "y.ts", {"x.ts": "import"})
    conn.commit()
    assert any(set(c["members"]) == {"x.ts", "y.ts"} for c in insights.cycles(conn)["cycles"])


def test_orphans_respects_script_entrypoints(project):
    _, conn = project
    for p in ["seeder.ts", "lib.ts"]:
        db.upsert_file(conn, p, "typescript", "h", 1.0, 10, "t")
    # seeder.ts matches no entrypoint convention, so it'd be dead...
    assert "seeder.ts" in insights.orphans(conn)["dead"]
    # ...until package.json scripts (recorded in meta) mark it as run-by-tooling
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('script_entrypoints',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(["seeder.ts"]),),
    )
    conn.commit()
    r = insights.orphans(conn)
    assert "seeder.ts" in r["entrypoints"]
    assert "seeder.ts" not in r["dead"]


def test_looks_entrypoint_recognizes_dart_conventions():
    assert insights.looks_entrypoint("lib/main.dart")              # Flutter app entry
    assert insights.looks_entrypoint("lib/main_dev.dart")          # flavor entry
    assert insights.looks_entrypoint("test/widget_test.dart")      # _test.dart
    assert insights.looks_entrypoint("integration_test/app_test.dart")
    assert insights.looks_entrypoint("bin/server.dart")            # Dart executable
    assert not insights.looks_entrypoint("lib/home_screen.dart")   # ordinary file


def test_dart_main_is_entrypoint_not_orphan(project):
    _, conn = project
    db.upsert_file(conn, "lib/main.dart", "dart", "h", 1.0, 10, "t")
    db.upsert_file(conn, "lib/home.dart", "dart", "h", 1.0, 10, "t")
    db.replace_edges(conn, "lib/main.dart", ["lib/home.dart"])  # main imports home
    conn.commit()
    r = insights.orphans(conn)
    assert "lib/main.dart" in r["entrypoints"]   # runtime entry, not dead code
    assert "lib/main.dart" not in r["dead"]


def test_flutter_lifecycle_methods_not_flagged_dead(project):
    _, conn = project
    db.upsert_file(conn, "lib/home.dart", "dart", "h", 1.0, 10, "t")
    db.replace_symbols(conn, "lib/home.dart", [
        ("method", "build", 3, "Widget build(BuildContext c)"),
        ("method", "initState", 5, "void initState()"),
        ("method", "props", 7, "List get props"),
        ("method", "computeUnusedThing", 9, "int computeUnusedThing()"),
    ])
    db.replace_refs(conn, "lib/home.dart", [])  # nothing referenced anywhere
    conn.commit()
    dead = {(d["kind"], d["name"]) for d in insights.dead_symbols(conn)["dead"]}
    assert ("method", "build") not in dead             # framework-invoked override
    assert ("method", "initState") not in dead
    assert ("method", "props") not in dead             # Equatable contract
    assert ("method", "computeUnusedThing") in dead    # genuinely unreferenced
