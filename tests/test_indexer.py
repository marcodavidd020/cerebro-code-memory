from cerebro import db, graph, indexer


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_file_hash_changes_with_content(tmp_path):
    p = write(tmp_path, "a.txt", "one")
    h1 = indexer.file_hash(p)
    p.write_text("two")
    assert indexer.file_hash(p) != h1


def test_extract_python_symbols_and_names_are_clean():
    src = (
        "# accents and an em-dash — should not shift byte offsets\n"
        "import os\n"
        "from a.b import c\n"
        "def login_user(creds):\n"
        "    return True\n"
        "class Auth:\n"
        "    def verify(self):\n"
        "        return 1\n"
    )
    symbols, imports, _calls, _refs = indexer.extract("python", src)
    names = {name for (_, name, _, _) in symbols}
    # the em-dash on line 1 must not corrupt later names (byte vs str regression)
    assert "login_user" in names
    assert "Auth" in names
    assert "verify" in names
    # imports captured (absolute module + imported name candidate)
    flat = {m for (_, m) in imports}
    assert "os" in flat
    assert any("a.b" in m for m in flat)


def test_extract_typescript():
    src = (
        "import {x} from './foo';\n"
        "export function handler() { return 1; }\n"
        "class Widget {}\n"
        "const build = () => 2;\n"
    )
    symbols, imports, _calls, _refs = indexer.extract("typescript", src)
    names = {name for (_, name, _, _) in symbols}
    assert {"handler", "Widget", "build"} <= names
    # imports are (source, is_type) pairs; a plain value import is runtime
    assert ("./foo", False) in set(imports)


def test_extract_typescript_dynamic_and_type_imports():
    src = (
        "import type { Cfg } from './types';\n"          # type-only
        "import { type A, B } from './mixed';\n"          # has a value (B) -> runtime
        "const Lazy = dynamic(() => import('./Lazy'));\n"  # dynamic -> runtime
        "const dep = require('./legacy');\n"               # commonjs -> runtime
    )
    _symbols, imports, _calls, _refs = indexer.extract("tsx", src)
    by_source = dict(imports)
    assert by_source["./types"] is True            # type-only import detected
    assert by_source["./mixed"] is False           # mixed clause stays runtime
    assert by_source["./Lazy"] is False            # dynamic import now captured
    assert by_source["./legacy"] is False          # require still captured


def test_extract_dart_symbols_imports_and_calls():
    src = (
        "import 'package:flutter/material.dart';\n"   # external package
        "import 'dart:async';\n"                       # SDK
        "import '../models/user.dart';\n"              # relative
        "void main() { runApp(const MyApp()); }\n"
        "String greet(String n) => 'Hi $n';\n"
        "class MyApp extends StatelessWidget {\n"
        "  Widget build(BuildContext c) { return Text(greet('x')); }\n"
        "}\n"
        "mixin Logger { void log(String m) {} }\n"
        "enum Color { red, green }\n"
    )
    symbols, imports, calls, refs = indexer.extract("dart", src)
    by_name = {name: kind for (kind, name, _, _) in symbols}
    assert by_name["main"] == "function"
    assert by_name["greet"] == "function"
    assert by_name["MyApp"] == "class"
    assert by_name["build"] == "method"       # method, not top-level function
    assert by_name["Logger"] == "class"       # mixin surfaces as a class-like symbol
    assert by_name["log"] == "method"
    assert by_name["Color"] == "enum"
    # every directive's URI is captured (resolution decides which become edges)
    assert "package:flutter/material.dart" in imports
    assert "../models/user.dart" in imports
    # calls keep their caller despite Dart's signature/body split
    assert ("main", "runApp") in {(enc, callee) for (enc, callee, _) in calls}
    assert ("build", "Text") in {(enc, callee) for (enc, callee, _) in calls}
    assert "greet" in refs and "Text" in refs


def test_extract_dart_typedefs_variables_enum_members_and_part():
    src = (
        "part 'user.g.dart';\n"                              # part directive -> edge
        "part of 'lib.dart';\n"                              # reverse pointer -> no edge
        "typedef Json = Map<String, dynamic>;\n"             # typedef
        "final userProvider = Provider((ref) => Svc());\n"   # top-level var (provider)
        "const apiUrl = 'x';\n"
        "enum Status { active, inactive }\n"                 # enum + members
        "class Model {\n"
        "  final String id;\n"                               # class field -> NOT a variable
        "}\n"
    )
    symbols, imports, calls, _refs = indexer.extract("dart", src)
    kinds = {}
    for kind, name, _line, _sig in symbols:
        kinds.setdefault(kind, set()).add(name)
    assert kinds["typedef"] == {"Json"}
    assert kinds["variable"] == {"userProvider", "apiUrl"}   # top-level only
    assert "id" not in kinds.get("variable", set())          # class field excluded
    assert kinds["enum_member"] == {"active", "inactive"}
    # part directive is an edge URI; `part of` is not
    assert "user.g.dart" in imports
    assert "lib.dart" not in imports
    # the provider's initializer calls are attributed to the provider
    assert ("userProvider", "Provider") in {(enc, c) for (enc, c, _l) in calls}


def test_dart_part_directive_deorphans_generated_file(tmp_path, project):
    from cerebro import insights

    config, conn = project
    write(tmp_path, "pubspec.yaml", "name: app\nversion: 1.0.0\n")
    write(
        tmp_path,
        "lib/user.dart",
        "part 'user.g.dart';\n"
        "class User {\n  factory User.fromJson(Map j) => _$UserFromJson(j);\n}\n",
    )
    write(
        tmp_path,
        "lib/user.g.dart",
        "part of 'user.dart';\nUser _$UserFromJson(Map j) => User.fromJson(j);\n",
    )
    indexer.reindex(config, conn)
    edges = {(r["src_path"], r["dst_path"]) for r in conn.execute("SELECT src_path, dst_path FROM edges")}
    assert ("lib/user.dart", "lib/user.g.dart") in edges     # part pulls the generated file in
    # ...so the generated file is not reported as a dead orphan
    assert "lib/user.g.dart" not in insights.orphans(conn)["dead"]


def test_dart_tool_cache_is_ignored(tmp_path, project):
    config, conn = project
    write(tmp_path, "pubspec.yaml", "name: app\nversion: 1.0.0\n")
    write(tmp_path, "lib/main.dart", "void main() {}\n")
    write(tmp_path, ".dart_tool/package_config.json", "{}\n")
    indexer.reindex(config, conn)
    assert not any(".dart_tool" in p for p in db.stored_hashes(conn))


def test_extract_dart_constructors_getters_setters():
    src = (
        "class Product {\n"
        "  final String name;\n"                          # field -> not a symbol
        "  Product(this.name);\n"                          # default ctor
        "  Product.named(this.name);\n"                    # named ctor
        "  const Product.zero() : name = '';\n"            # const named ctor
        "  factory Product.fromJson(Map j) => Product(j['n']);\n"  # factory ctor
        "  double get price => 0.0;\n"                     # getter
        "  set price(double v) {}\n"                       # setter
        "  void describe() {}\n"                           # method
        "}\n"
    )
    symbols, _imports, calls, _refs = indexer.extract("dart", src)
    by_kind = {}
    for kind, name, _line, _sig in symbols:
        by_kind.setdefault(kind, set()).add(name)
    # constructors use their simple call-site name: default -> class name, the
    # rest -> their own name part (so cerebro_callers/dead_symbols can match them)
    assert by_kind["constructor"] == {"Product", "named", "zero", "fromJson"}
    assert by_kind["class"] == {"Product"}
    assert {"price", "describe"} <= by_kind["method"]   # getter+setter+method
    # a plain field is not emitted as a symbol
    assert "name" not in {n for names in by_kind.values() for n in names}
    # the factory body's Product(...) call is attributed to the factory ctor
    assert ("fromJson", "Product") in {(enc, callee) for (enc, callee, _l) in calls}


def test_dart_named_constructor_callers_and_dead(tmp_path, project):
    from cerebro import callgraph, insights

    config, conn = project
    write(tmp_path, "pubspec.yaml", "name: app\nversion: 1.0.0\n")
    write(
        tmp_path,
        "lib/user.dart",
        "class User {\n"
        "  final String name;\n"
        "  User(this.name);\n"
        "  factory User.fromJson(Map j) => User(j['name']);\n"
        "  User.unused() : name = 'x';\n"
        "}\n",
    )
    write(
        tmp_path,
        "lib/login.dart",
        "import 'package:app/user.dart';\n"
        "class Login {\n"
        "  void run(Map d) { final u = User.fromJson(d); }\n"
        "}\n",
    )
    indexer.reindex(config, conn)
    # the named (factory) constructor's caller is found by simple name
    assert ("lib/login.dart", "run", 3) in callgraph.callers(conn, "fromJson")["sites"]
    dead = {(d["kind"], d["name"]) for d in insights.dead_symbols(conn)["dead"]
            if d["path"] == "lib/user.dart"}
    assert ("constructor", "fromJson") not in dead   # used -> alive
    assert ("constructor", "unused") in dead          # never constructed -> dead


def test_extract_dart_method_and_cascade_calls():
    src = (
        "void demo(Cart cart, BuildContext context) {\n"
        "  cart.add(Product('x'));\n"            # method call on an object
        "  final t = cart.total();\n"            # method call, no args
        "  Navigator.of(context).push(route);\n"  # chained method calls
        "  final m = context.read<Model>();\n"   # generic method call
        "  obj.a.b.deep(1);\n"                    # only the invoked tail is a call
        "  list..add(1)..add(2);\n"              # cascade invocations
        "  setState(() {});\n"                    # bare call
        "}\n"
    )
    _symbols, _imports, calls, _refs = indexer.extract("dart", src)
    callees = [callee for (_enc, callee, _line) in calls]
    # method calls on objects are captured (the call-graph payload)...
    assert {"add", "total", "of", "push", "read", "deep", "setState"} <= set(callees)
    # ...the cascade invokes add twice, plus the cart.add once -> three total
    assert callees.count("add") == 3
    # field accesses in obj.a.b.deep(1) are reads, not calls
    assert "a" not in callees and "b" not in callees
    # every call is attributed to the enclosing function, not the module
    assert all(enc == "demo" for (enc, _c, _l) in calls)


def test_dart_method_call_has_caller_in_callgraph(tmp_path, project):
    from cerebro import callgraph

    config, conn = project
    write(tmp_path, "pubspec.yaml", "name: app\nversion: 1.0.0\n")
    write(
        tmp_path,
        "lib/cart_service.dart",
        "class CartService {\n  void add(String item) {}\n}\n",
    )
    write(
        tmp_path,
        "lib/cart_page.dart",
        "import 'package:app/cart_service.dart';\n"
        "class CartPage {\n"
        "  void render() {\n"
        "    final cart = CartService();\n"
        "    cart.add('coffee');\n"
        "  }\n"
        "}\n",
    )
    indexer.reindex(config, conn)
    res = callgraph.callers(conn, "add")
    assert res["count"] == 1
    assert ("lib/cart_page.dart", "render", 5) in res["sites"]  # called by render(), not the class


def test_reindex_resolves_dart_package_and_relative_imports(tmp_path, project):
    config, conn = project
    write(tmp_path, "myapp/pubspec.yaml", "name: myapp\nversion: 1.0.0\n")
    write(tmp_path, "myapp/lib/widgets/button.dart", "class Button {}\n")
    write(tmp_path, "myapp/lib/models/user.dart", "class User {}\n")
    write(
        tmp_path,
        "myapp/lib/main.dart",
        "import 'package:myapp/widgets/button.dart';\n"  # package-self import
        "import 'models/user.dart';\n"                    # bare relative import
        "import 'package:flutter/material.dart';\n"       # external -> no edge
        "void main() { Button(); User(); }\n",
    )
    indexer.reindex(config, conn)
    edges = {(r["src_path"], r["dst_path"]) for r in conn.execute("SELECT src_path, dst_path FROM edges")}
    assert ("myapp/lib/main.dart", "myapp/lib/widgets/button.dart") in edges  # package:self
    assert ("myapp/lib/main.dart", "myapp/lib/models/user.dart") in edges     # relative
    # the external package import produced no edge (no local target)
    assert not any(d.startswith("flutter") for (_, d) in edges)
    # graph sees button/user as depended upon by main
    assert "myapp/lib/main.dart" in graph.dependents(conn, "myapp/lib/widgets/button.dart")


def test_resolve_imports_marks_type_only_edges(tmp_path, project):
    config, conn = project
    write(tmp_path, "types.ts", "export type Cfg = { a: number };\n")
    write(tmp_path, "widget.tsx", "export const W = () => 1;\n")
    write(
        tmp_path,
        "main.ts",
        "import type { Cfg } from './types';\n"
        "const W = dynamic(() => import('./widget'));\n",
    )
    indexer.reindex(config, conn)
    kinds = {
        (r["src_path"], r["dst_path"]): r["kind"]
        for r in conn.execute("SELECT src_path, dst_path, kind FROM edges")
    }
    assert kinds[("main.ts", "types.ts")] == "type"     # type-only edge tagged
    assert kinds[("main.ts", "widget.tsx")] == "import"  # dynamic import is runtime


def test_reindex_builds_index_edges_and_graph(tmp_path, project):
    config, conn = project
    write(tmp_path, "app/util.py", "def helper():\n    return 1\n")
    write(
        tmp_path,
        "app/main.py",
        "from app.util import helper\nfrom . import util\n\ndef run():\n    return helper()\n",
    )
    result = indexer.reindex(config, conn)
    assert result["new"] == 2

    # absolute + relative import both resolve to the same target
    edges = {(r["src_path"], r["dst_path"]) for r in conn.execute("SELECT src_path, dst_path FROM edges")}
    assert ("app/main.py", "app/util.py") in edges

    # util is depended upon -> ranks above main; main imports util
    assert graph.dependents(conn, "app/util.py") == ["app/main.py"]
    assert "app/util.py" in graph.dependencies(conn, "app/main.py")
    ranked = dict(graph.rank(conn))
    assert ranked["app/util.py"] > ranked["app/main.py"]


def test_reindex_is_incremental_and_detects_deletion(tmp_path, project):
    config, conn = project
    write(tmp_path, "a.py", "def a():\n    return 1\n")
    write(tmp_path, "b.py", "def b():\n    return 2\n")
    indexer.reindex(config, conn)

    # change a.py, delete b.py
    write(tmp_path, "a.py", "def a():\n    return 99\n\ndef a2():\n    return 0\n")
    (tmp_path / "b.py").unlink()

    disk = indexer.disk_state(config)
    d = indexer.diff(conn, disk)
    assert d["changed"] == ["a.py"]
    assert d["deleted"] == ["b.py"]

    result = indexer.reindex(config, conn)
    assert result["changed"] == 1
    assert result["deleted"] == 1
    assert set(db.stored_hashes(conn)) == {"a.py"}
    # a.py's new symbol is present after incremental reindex
    assert {r["name"] for r in db.symbols_for(conn, "a.py")} == {"a", "a2"}


def test_reindex_paths_incremental_single_file(tmp_path, project):
    config, conn = project
    write(tmp_path, "a.py", "def a():\n    return 1\n")
    write(tmp_path, "b.py", "def b():\n    return 2\n")
    indexer.reindex(config, conn)

    # edit only a.py, then incrementally reindex just that file (no full walk)
    write(tmp_path, "a.py", "def a():\n    return 1\n\ndef a2():\n    return 0\n")
    result = indexer.reindex_paths(config, conn, ["a.py"])
    assert result["touched"] == 1
    assert {r["name"] for r in db.symbols_for(conn, "a.py")} == {"a", "a2"}
    # b.py untouched and still present
    assert {r["name"] for r in db.symbols_for(conn, "b.py")} == {"b"}

    # deleting then reindex_paths the path removes it
    (tmp_path / "a.py").unlink()
    indexer.reindex_paths(config, conn, ["a.py"])
    assert "a.py" not in db.stored_hashes(conn)


def test_reindex_paths_resolves_edges_against_index(tmp_path, project):
    config, conn = project
    write(tmp_path, "util.py", "def helper():\n    return 1\n")
    write(tmp_path, "main.py", "x = 1\n")
    indexer.reindex(config, conn)
    # main.py gains an import; incremental reindex must still resolve the edge
    write(tmp_path, "main.py", "from util import helper\n")
    indexer.reindex_paths(config, conn, ["main.py"])
    edges = {(r["src_path"], r["dst_path"]) for r in conn.execute("SELECT src_path, dst_path FROM edges")}
    assert ("main.py", "util.py") in edges


def test_dead_symbols_end_to_end(tmp_path, project):
    from cerebro import insights

    config, conn = project
    write(
        tmp_path,
        "proj/lib.ts",
        "export function used() { return 1; }\nexport function deadOne() { return 2; }\n",
    )
    write(
        tmp_path,
        "proj/app.ts",
        "import { used } from './lib';\nexport function run() { return used(); }\n",
    )
    indexer.reindex(config, conn)
    dead = {(d["path"], d["name"]) for d in insights.dead_symbols(conn)["dead"]}
    assert ("proj/lib.ts", "deadOne") in dead   # exported but imported/used nowhere
    assert ("proj/lib.ts", "used") not in dead   # imported and called within the project
    # a self-call must not count as a use that revives a dead symbol
    assert ("proj/app.ts", "run") in dead        # run() defined, referenced nowhere


def test_reindex_records_package_json_script_entrypoints(tmp_path, project):
    from cerebro import insights

    config, conn = project
    write(tmp_path, "backend/src/database/seeder.ts", "export const run = () => 1;\n")
    write(tmp_path, "backend/src/util.ts", "export const x = 1;\n")
    write(
        tmp_path,
        "backend/package.json",
        '{"scripts": {"seed": "ts-node -r tsconfig-paths/register src/database/seeder.ts"}}\n',
    )
    indexer.reindex(config, conn)
    # the script-referenced file is resolved repo-relative and recorded...
    assert "backend/src/database/seeder.ts" in insights.script_entrypoints(conn)
    # ...so orphans treats it as an entrypoint, not dead code
    r = insights.orphans(conn)
    assert "backend/src/database/seeder.ts" in r["entrypoints"]
    assert "backend/src/database/seeder.ts" not in r["dead"]


def test_ignored_dirs_are_skipped(tmp_path, project):
    config, conn = project
    write(tmp_path, "keep.py", "def k():\n    return 1\n")
    write(tmp_path, "node_modules/dep/index.js", "export const z = 1;\n")
    write(tmp_path, "__pycache__/junk.py", "x = 1\n")
    indexer.reindex(config, conn)
    assert set(db.stored_hashes(conn)) == {"keep.py"}
