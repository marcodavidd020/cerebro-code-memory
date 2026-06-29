"""Static indexer: hash files, extract symbols + imports, resolve dependency edges.

No LLM is involved here — this layer is deterministic, fast, and free. It is the
structural map (plan layers 1 and 5). Summaries (layer 2) are written separately
by the chat sessions via summaries.record().
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import db
from . import tsconfig

try:
    from tree_sitter_language_pack import get_parser
except Exception:  # pragma: no cover - import guard for environments w/o the pack
    get_parser = None

# tree-sitter's Parser objects (Rust/pyo3 binding) are unsendable across threads —
# sharing one cache between threads panics. FastMCP runs sync tools in a worker
# thread pool, so the cache must be thread-local.
_PARSER_TLS = threading.local()


def _parser(lang: str):
    if get_parser is None:
        return None
    cache = getattr(_PARSER_TLS, "parsers", None)
    if cache is None:
        cache = {}
        _PARSER_TLS.parsers = cache
    if lang not in cache:
        try:
            cache[lang] = get_parser(lang)
        except Exception:
            cache[lang] = None
    return cache[lang]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --- disk diff ---------------------------------------------------------------

def disk_state(config: cfg.Config) -> dict[str, str]:
    """Map relative path -> current on-disk hash for all indexable files."""
    state: dict[str, str] = {}
    for rel, abs_path in config.iter_files():
        try:
            state[rel] = file_hash(abs_path)
        except OSError:
            continue
    return state


def diff(conn, disk: dict[str, str]) -> dict[str, list[str]]:
    stored = db.stored_hashes(conn)
    new = [p for p in disk if p not in stored]
    changed = [p for p in disk if p in stored and disk[p] != stored[p]]
    deleted = [p for p in stored if p not in disk]
    return {"new": sorted(new), "changed": sorted(changed), "deleted": sorted(deleted)}


# --- tree-sitter node accessors ----------------------------------------------
# tree-sitter-language-pack ships a binding whose Node members are *methods*
# (node.kind(), node.start_byte()) rather than the properties of the standard
# py-tree-sitter package (node.type, node.start_byte). These helpers normalize
# both so the extraction logic stays clean and version-resilient.

def _attr(node, *names):
    for name in names:
        if hasattr(node, name):
            v = getattr(node, name)
            return v() if callable(v) else v
    raise AttributeError(f"node has none of {names}")


def _kind(n) -> str:
    return _attr(n, "kind", "type")


def _child_count(n) -> int:
    return _attr(n, "child_count")


def _children(n):
    return [n.child(i) for i in range(_child_count(n))]


def _field(n, name):
    return n.child_by_field_name(name)


def _line(n) -> int:
    pos = _attr(n, "start_position", "start_point")
    return getattr(pos, "row", 0) + 1


def _text(src: bytes, n) -> str:
    # tree-sitter reports *byte* offsets; slice the UTF-8 bytes, never the str,
    # or multi-byte chars (em-dashes, emoji) shift every later offset.
    return src[_attr(n, "start_byte") : _attr(n, "end_byte")].decode("utf-8", "ignore")


def _root(tree):
    return _attr(tree, "root_node")


def _do_parse(parser, text: str):
    try:
        return parser.parse(text)  # language-pack binding wants str
    except TypeError:
        return parser.parse(text.encode("utf-8"))  # standard binding wants bytes


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(_children(n))


# --- symbol / import extraction ----------------------------------------------

_DEF_TYPES = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
}
_ARROW_VALUES = ("arrow_function", "function", "function_expression")


def _base_lang(lang: str) -> str:
    """Collapse a tree-sitter language name to the extractor family that handles
    it. typescript/tsx/javascript all share the JS extractor; dart and python
    each have their own."""
    if lang == "python":
        return "python"
    if lang == "dart":
        return "dart"
    return "javascript"
# Identifier node kinds we count as a *use* of a name (a reference). Definition
# sites are excluded separately, so what remains is genuine usage: calls, JSX
# tags, type annotations, value reads, object shorthand.
_IDENT_KINDS = {
    "identifier", "property_identifier", "type_identifier", "shorthand_property_identifier",
}


def _signature(src: str, node) -> str:
    text = _text(src, node)
    line = text.splitlines()[0] if text else ""
    return line.strip()[:160]


def extract(lang: str, src):
    """Return (symbols, imports, calls, refs).
    symbols: (kind, name, line, signature). imports: language-specific descriptors.
    calls: (enclosing_symbol|None, callee_name, line). refs: distinct names USED in
    the file (references), with the file's own definition names excluded."""
    if isinstance(src, bytes):
        src_bytes, src_str = src, src.decode("utf-8", "ignore")
    else:
        src_str, src_bytes = src, src.encode("utf-8")
    parser = _parser(lang)
    if parser is None:
        return [], [], [], []
    root = _root(_do_parse(parser, src_str))
    base = _base_lang(lang)
    if base == "dart":
        return _dart_extract(root, src_bytes)
    def_types = _DEF_TYPES[base]
    call_kinds = {"call"} if base == "python" else {"call_expression", "new_expression"}

    symbols, calls = [], []
    refs: set[str] = set()
    def_spans: set[int] = set()  # byte offsets of definition-name nodes (not uses)
    stack = [(root, None)]  # (node, enclosing definition name)
    while stack:
        n, enc = stack.pop()
        k = _kind(n)
        new_enc = enc
        kind = def_types.get(k)
        if kind:
            name_node = _field(n, "name")
            if name_node is not None:
                nm = _text(src_bytes, name_node)
                symbols.append((kind, nm, _line(n), _signature(src_bytes, n)))
                def_spans.add(_attr(name_node, "start_byte"))
                new_enc = nm
        # const foo = () => {...}  /  const Bar = function(){}
        elif k == "variable_declarator":
            value = _field(n, "value")
            name_node = _field(n, "name")
            if name_node is not None:
                def_spans.add(_attr(name_node, "start_byte"))  # a binding, not a use
                if value is not None and _kind(value) in _ARROW_VALUES:
                    nm = _text(src_bytes, name_node)
                    symbols.append(("function", nm, _line(n), _signature(src_bytes, n)))
                    new_enc = nm
        elif k in call_kinds:
            cn = _callee_name(src_bytes, n)
            if cn:
                calls.append((enc, cn, _line(n)))
        if k in _IDENT_KINDS and _attr(n, "start_byte") not in def_spans:
            refs.add(_text(src_bytes, n))
        for c in _children(n):
            stack.append((c, new_enc))

    imports = _py_imports(root, src_bytes) if base == "python" else _js_imports(root, src_bytes)
    return symbols, imports, calls, sorted(refs)


def _callee_name(src, call):
    """Best-effort callee name of a call / new expression (the rightmost name)."""
    fn = _field(call, "function") or _field(call, "constructor")
    if fn is None:
        return None
    k = _kind(fn)
    if k in ("identifier", "property_identifier"):
        return _text(src, fn)
    if k == "attribute":  # python a.b.c -> field 'attribute'
        a = _field(fn, "attribute")
        return _text(src, a) if a is not None else None
    if k == "member_expression":  # js a.b.c -> field 'property'
        p = _field(fn, "property")
        return _text(src, p) if p is not None else None
    return None


def _aliased_name(src, node):
    name = _field(node, "name") or next(
        (g for g in _children(node) if _kind(g) == "dotted_name"), None
    )
    return _text(src, name) if name is not None else None


def _py_imports(root, src):
    """Return list of (level, module): level 0 == absolute, level N == N leading dots.

    For `from <module> import <names>` we also emit each imported name as a
    candidate submodule (joined to the module). This is what resolves
    `from . import config` -> config.py, since tree-sitter puts the dots in
    module_name and the name `config` in a separate child.
    """
    out = []
    for n in _walk(root):
        k = _kind(n)
        if k == "import_statement":
            for c in _children(n):
                ck = _kind(c)
                if ck == "dotted_name":
                    out.append((0, _text(src, c)))
                elif ck == "aliased_import":
                    nm = _aliased_name(src, c)
                    if nm:
                        out.append((0, nm))
        elif k == "import_from_statement":
            mod = _field(n, "module_name")
            if mod is None:
                continue
            mtxt = _text(src, mod)
            level = len(mtxt) - len(mtxt.lstrip("."))
            module = mtxt[level:]
            out.append((level, module))
            mod_span = (_attr(mod, "start_byte"), _attr(mod, "end_byte"))
            for c in _children(n):
                if (_attr(c, "start_byte"), _attr(c, "end_byte")) == mod_span:
                    continue
                ck = _kind(c)
                nm = None
                if ck == "dotted_name":
                    nm = _text(src, c)
                elif ck == "aliased_import":
                    nm = _aliased_name(src, c)
                if nm:
                    out.append((level, f"{module}.{nm}" if module else nm))
    return out


def _is_type_only(stmt) -> bool:
    """True when a TS import/export contributes no runtime value, so its edge is
    elided after compilation: `import type {...}` (a `type` keyword right after
    `import`), or a named clause where every specifier is itself `type`-qualified
    (`import { type A, type B }`). A default/namespace binding, a side-effect
    import, or any untyped specifier makes it a real runtime edge."""
    children = _children(stmt)
    if any(_kind(c) == "type" for c in children):  # `import type { ... } from ...`
        return True
    specifiers = []
    for c in children:
        if _kind(c) != "import_clause":
            continue
        for cc in _children(c):
            ck = _kind(cc)
            if ck == "named_imports":
                specifiers.extend(s for s in _children(cc) if _kind(s) == "import_specifier")
            elif ck in ("identifier", "namespace_import"):
                return False  # default / `* as ns` binding is a runtime value
    if not specifiers:
        return False  # side-effect import, or a re-export we can't prove type-only
    return all(any(_kind(g) == "type" for g in _children(s)) for s in specifiers)


def _js_imports(root, src):
    """Return list of (source, is_type) — source strings like './foo' or 'react',
    is_type=True for type-only TS imports (no runtime edge). Captures static
    import/export, CommonJS `require(...)`, and dynamic `import(...)` (e.g. the
    `dynamic(() => import('./X'))` lazy-load pattern), all as runtime edges."""
    out = []
    for n in _walk(root):
        k = _kind(n)
        if k in ("import_statement", "export_statement"):
            source = _field(n, "source")
            if source is not None:
                out.append((_text(src, source).strip("\"'`"), _is_type_only(n)))
        elif k == "call_expression":
            fn = _field(n, "function")
            if fn is None:
                continue
            # require('x')  or  dynamic import('x') (function node is the `import` kw)
            if _text(src, fn) == "require" or _kind(fn) == "import":
                args = _field(n, "arguments")
                if args is not None:
                    for c in _children(args):
                        if _kind(c) == "string":
                            out.append((_text(src, c).strip("\"'`"), False))
    return out


# --- Dart / Flutter extraction -----------------------------------------------
# tree-sitter-dart shape (verified against the language pack grammar):
#   top-level function  -> `function_signature` (name field); body is a sibling
#   method in a class   -> `method_signature` wrapping a `function_signature`
#   class / enum        -> `class_definition` / `enum_declaration` (name field)
#   mixin               -> `mixin_declaration` (no name field; plain identifier)
#   extension           -> `extension_declaration` (may be anonymous -> no symbol)
#   call `Foo(...)`     -> an `identifier`/`type_identifier` immediately followed
#                          by a `selector` whose first child is `argument_part`
_DART_CONTAINER_DEFS = {
    "class_definition": "class",
    "mixin_declaration": "class",
    "enum_declaration": "enum",
    "extension_declaration": "extension",
}
_DART_IDENT_KINDS = {"identifier", "type_identifier"}


def _dart_name_node(node):
    """Name node of a Dart container declaration: the `name` field when present
    (class/enum/named extension), else the first plain `identifier` child (mixin).
    Anonymous extensions have neither, so this returns None and no symbol is emitted
    — never the `type_identifier` of the extended type."""
    nm = _field(node, "name")
    if nm is None:
        nm = next((c for c in _children(node) if _kind(c) == "identifier"), None)
    return nm


def _dart_imports(root, src):
    """Return the raw URI string of every import/export/part directive, e.g.
    'package:app/x.dart', '../models/user.dart', 'user.g.dart'. Exports re-expose a
    file's API, and `part` pulls a file into the library, so both are real edges.
    `part of` is the reverse pointer (part -> library); we skip it to avoid a cycle
    against the `part` edge the library already declares."""
    out = []
    for n in _walk(root):
        if _kind(n) in ("library_import", "library_export", "part_directive"):
            uri = next((c for c in _walk(n) if _kind(c) == "uri"), None)
            if uri is not None:
                out.append(_text(src, uri).strip().strip("\"'"))
    return out


# Signatures that sit inside a class-body member wrapper. A member with a body
# (or abstract) is a `method_signature`; a field or body-less constructor is a
# `declaration`. Either can wrap one of these inner signatures.
_DART_SIG_KINDS = {
    "function_signature": "method",
    "constructor_signature": "constructor",
    "constant_constructor_signature": "constructor",
    "factory_constructor_signature": "constructor",
    "getter_signature": "method",
    "setter_signature": "method",
}


def _dart_member_symbol(node, src):
    """Resolve a class-body member (`method_signature` or `declaration`) to a
    (kind, name, name_nodes) triple. The name is the *simple* call-site name — the
    last plain identifier of the signature — so `Product.named` -> 'named', the
    default ctor -> the class name, a getter -> its property. Matching by simple
    name is what keeps cerebro_callers and dead_symbols working, since the refs and
    calls tables store unqualified names. Returns (None, None, ()) for fields and
    other declarations that define no callable symbol."""
    sig = next((c for c in _children(node) if _kind(c) in _DART_SIG_KINDS), None)
    if sig is None:
        return None, None, ()
    if _kind(sig) == "function_signature":
        nm = _field(sig, "name")
        return ("method", _text(src, nm), (nm,)) if nm is not None else (None, None, ())
    # ctor / getter / setter: name is the signature's last direct identifier
    # (`Product.named` -> [Product, named] -> 'named'; getter -> [total]).
    idents = [c for c in _children(sig) if _kind(c) == "identifier"]
    if not idents:
        return None, None, ()
    return _DART_SIG_KINDS[_kind(sig)], _text(src, idents[-1]), tuple(idents)


def _dart_extract(root, src):
    """Dart's grammar differs enough from python/js (methods wrap a nested
    function_signature; calls are identifier+selector, not call_expression) to
    warrant its own walk. Returns the same (symbols, imports, calls, refs)."""
    symbols, calls = [], []
    refs: set[str] = set()
    def_spans: set[int] = set()  # byte offsets of definition-name nodes (not uses)
    stack = [(root, None, None)]  # (node, parent_kind, enclosing definition name)
    while stack:
        n, parent_kind, enc = stack.pop()
        k = _kind(n)
        new_enc = enc
        # Members (methods, constructors, getters, setters) live in a
        # method_signature (has a body / abstract) or a declaration (field or
        # body-less ctor); _dart_member_symbol sorts out which and skips fields.
        if k in ("method_signature", "declaration"):
            mkind, mname, mnodes = _dart_member_symbol(n, src)
            if mkind:
                symbols.append((mkind, mname, _line(n), _signature(src, n)))
                for nn in mnodes:
                    def_spans.add(_attr(nn, "start_byte"))
                new_enc = mname
        elif k == "function_signature" and parent_kind not in ("method_signature", "declaration"):
            name_node = _field(n, "name")  # top-level fn; member sigs handled above
            if name_node is not None:
                nm = _text(src, name_node)
                symbols.append(("function", nm, _line(n), _signature(src, n)))
                def_spans.add(_attr(name_node, "start_byte"))
                new_enc = nm
        elif k in _DART_CONTAINER_DEFS:
            name_node = _dart_name_node(n)
            if name_node is not None:
                nm = _text(src, name_node)
                symbols.append((_DART_CONTAINER_DEFS[k], nm, _line(n), _signature(src, n)))
                def_spans.add(_attr(name_node, "start_byte"))
                new_enc = nm
        elif k == "type_alias":  # typedef Json = ...;  -> name is the first type_identifier
            name_node = next((c for c in _children(n) if _kind(c) == "type_identifier"), None)
            if name_node is not None:
                symbols.append(("typedef", _text(src, name_node), _line(n), _signature(src, n)))
                def_spans.add(_attr(name_node, "start_byte"))
        elif k in ("static_final_declaration_list", "initialized_identifier_list") and parent_kind == "program":
            # Top-level const/final/var (Riverpod providers, theme constants, etc.).
            # The same node kinds nest under `declaration` for class fields, which
            # the parent_kind=='program' guard excludes.
            decls = [c for c in _children(n)
                     if _kind(c) in ("static_final_declaration", "initialized_identifier")]
            names = [next((g for g in _children(d) if _kind(g) == "identifier"), None) for d in decls]
            for d, nm in zip(decls, names):
                if nm is not None:
                    symbols.append(("variable", _text(src, nm), _line(d), _signature(src, d)))
                    def_spans.add(_attr(nm, "start_byte"))
            # Attribute the initializer's calls to the variable when it's the only
            # one declared (`final x = Provider(...)` -> x calls Provider).
            if len(names) == 1 and names[0] is not None:
                new_enc = _text(src, names[0])
        elif k == "enum_constant":
            name_node = next((c for c in _children(n) if _kind(c) == "identifier"), None)
            if name_node is not None:
                symbols.append(("enum_member", _text(src, name_node), _line(n), _signature(src, n)))
                def_spans.add(_attr(name_node, "start_byte"))
        # --- call sites, all read from a node's direct children -------------
        #   bare call    Foo(...)      -> identifier + selector(argument_part)
        #   method call  obj.foo(...)  -> selector(.foo) + selector(argument_part)
        #   cascade      obj..foo(...) -> cascade_section{ cascade_selector + argument_part }
        # A `selector` only carries args when its first child is `argument_part`
        # (a `.name` field access is an unconditional_assignable_selector instead),
        # which is what tells `obj.foo(...)` (call) apart from `obj.foo` (read).
        kids = _children(n)
        if k == "cascade_section" and any(_kind(c) == "argument_part" for c in kids):
            csel = next((c for c in kids if _kind(c) == "cascade_selector"), None)
            nm = next((g for g in _children(csel) if _kind(g) == "identifier"), None) if csel else None
            if nm is not None:
                calls.append((enc, _text(src, nm), _line(nm)))
        for i, c in enumerate(kids):
            nxt = kids[i + 1] if i + 1 < len(kids) else None
            sc = _children(nxt) if nxt is not None and _kind(nxt) == "selector" else None
            if not (sc and _kind(sc[0]) == "argument_part"):
                continue
            name_node = None
            if _kind(c) in _DART_IDENT_KINDS:           # Foo(...), setState(...)
                name_node = c
            elif _kind(c) == "selector":                # obj.foo(...): name is in the
                uas = _children(c)[0] if _children(c) else None  # preceding .foo selector
                if uas is not None and _kind(uas) == "unconditional_assignable_selector":
                    name_node = next((g for g in _children(uas) if _kind(g) == "identifier"), None)
            if name_node is not None:
                calls.append((enc, _text(src, name_node), _line(name_node)))
        if k in _DART_IDENT_KINDS and _attr(n, "start_byte") not in def_spans:
            refs.add(_text(src, n))
        # A function/method body is a *sibling* of its signature in Dart, so its
        # calls would otherwise be attributed to the enclosing class (or to None
        # at top level). Pair each signature with the following body so calls
        # inside are attributed to that function/method.
        child_enc = {}
        pending = None
        for i, c in enumerate(kids):
            ck = _kind(c)
            if ck == "function_signature":
                nm = _field(c, "name")
                pending = _text(src, nm) if nm is not None else None
            elif ck in ("method_signature", "declaration"):
                pending = _dart_member_symbol(c, src)[1]
            elif ck == "function_body":
                if pending is not None:
                    child_enc[i] = pending
                pending = None
        for i, c in enumerate(kids):
            stack.append((c, k, child_enc.get(i, new_enc)))
    return symbols, _dart_imports(root, src), calls, sorted(refs)


# --- import resolution (raw import -> repo-relative path) --------------------

def _resolve_python(level, module, importer_rel, known: set[str]):
    parts_mod = [p for p in module.split(".") if p]
    if level > 0:
        importer_dir = posixpath.dirname(importer_rel)
        dir_parts = importer_dir.split("/") if importer_dir else []
        # level 1 = importer's own package; each extra dot climbs one more.
        keep = len(dir_parts) - (level - 1)
        if keep < 0:
            return None
        base = dir_parts[:keep] + parts_mod
        return _first_existing(["/".join(base)], known)
    candidate = "/".join(parts_mod)
    return _first_existing([candidate], known, suffix_ok=True)


def _first_existing(stems, known: set[str], suffix_ok: bool = False):
    for stem in stems:
        cands = [stem + ".py", stem + "/__init__.py"]
        for c in cands:
            if c in known:
                return c
        if suffix_ok:
            for c in cands:
                tail = "/" + c
                hit = next((k for k in known if k.endswith(tail)), None)
                if hit:
                    return hit
    return None


_JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]


def _resolve_fs(target: str, known: set[str]):
    """Resolve a repo-relative module path (no extension) to a real file, trying
    JS/TS extensions and an index file in a directory."""
    cands = [target] + [target + e for e in _JS_EXTS]
    cands += [target + "/index" + e for e in _JS_EXTS]
    for c in cands:
        if c in known:
            return c
    return None


def _resolve_js(source, importer_rel, known: set[str]):
    if not source.startswith("."):
        return None  # relative imports only; aliases handled separately
    importer_dir = posixpath.dirname(importer_rel)
    target = posixpath.normpath(posixpath.join(importer_dir, source))
    return _resolve_fs(target, known)


def _resolve_dart(uri: str, importer_rel: str, known: set[str], pkg_roots: dict):
    """Resolve a Dart import URI to a repo-relative file.
      'dart:...'      -> SDK, no edge.
      'package:p/sub' -> p's pubspec dir + '/lib/' + sub, when p is a local package.
      anything else   -> a path relative to the importer. Dart treats a bare
                         'src/x.dart' as relative (unlike a JS bare specifier,
                         which means node_modules)."""
    if uri.startswith("dart:"):
        return None
    if uri.startswith("package:"):
        pkg, _, sub = uri[len("package:"):].partition("/")
        base = pkg_roots.get(pkg)
        if base is None or not sub:
            return None
        target = posixpath.normpath(posixpath.join(base, "lib", sub))
        return target if target in known else None
    importer_dir = posixpath.dirname(importer_rel)
    target = posixpath.normpath(posixpath.join(importer_dir, uri))
    return target if target in known else None


def resolve_imports(lang, imports, importer_rel, known: set[str], alias_configs=None, dart_pkgs=None) -> dict:
    """Resolve raw imports to a {dst_path: kind} map. kind is 'type' only when
    EVERY import resolving to that target is type-only — a single runtime import
    makes the edge 'import', since the target is then loaded at runtime."""
    base = _base_lang(lang)
    if base == "dart":
        edges = {}
        for uri in imports:
            hit = _resolve_dart(uri, importer_rel, known, dart_pkgs or {})
            if hit and hit != importer_rel:
                edges[hit] = "import"
        return edges
    runtime, type_only = set(), set()
    for imp in imports:
        is_type = False
        if base == "python":
            level, module = imp
            hit = _resolve_python(level, module, importer_rel, known)
        else:
            source, is_type = imp
            if source.startswith("."):
                hit = _resolve_js(source, importer_rel, known)
            else:
                # bare import: try tsconfig/jsconfig path aliases (@/..., ~/..., etc.)
                hit = None
                for cand in tsconfig.expand(source, importer_rel, alias_configs or []):
                    hit = _resolve_fs(cand, known)
                    if hit:
                        break
        if hit and hit != importer_rel:
            (type_only if is_type else runtime).add(hit)
    edges = {d: "import" for d in runtime}
    for d in type_only:
        edges.setdefault(d, "type")  # demoted to runtime above if also imported as a value
    return edges


# --- framework entrypoints (loaded by tooling, not by import) ----------------

# A source-file token inside a package.json script command, e.g. the
# `src/database/seeder.ts` in `ts-node ... src/database/seeder.ts`.
_SCRIPT_FILE_RE = re.compile(r"[\w./@-]+\.(?:ts|tsx|js|jsx|mjs|cjs)\b")


def script_entrypoints(config, known: set[str]) -> set[str]:
    """Repo-relative source files invoked by a package.json `scripts` command.

    These are run by tooling (`npm run seed` -> `ts-node src/database/seeder.ts`),
    not imported by other code, so the dependency graph never sees an edge into
    them — without this they masquerade as dead code in orphans()."""
    out: set[str] = set()
    for rel in known:
        if posixpath.basename(rel) != "package.json":
            continue
        try:
            data = json.loads((config.root / rel).read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        scripts = data.get("scripts")
        if not isinstance(scripts, dict):
            continue
        pkg_dir = posixpath.dirname(rel)
        for cmd in scripts.values():
            if not isinstance(cmd, str):
                continue
            for token in _SCRIPT_FILE_RE.findall(cmd):
                target = posixpath.normpath(posixpath.join(pkg_dir, token))
                if target in known:
                    out.add(target)
    return out


# A pubspec.yaml top-level `name:` value (the Dart package name). Matched without
# a YAML parser since we only need this one scalar field.
_DART_PKG_NAME_RE = re.compile(r"(?m)^name:[ \t]*['\"]?([A-Za-z_][A-Za-z0-9_]*)")


def dart_package_roots(config, known: set[str]) -> dict:
    """Map each Dart package name to the repo-relative dir holding its pubspec.yaml,
    so `package:<name>/x.dart` imports resolve to `<dir>/lib/x.dart`. A Flutter
    monorepo/polyrepo can declare several packages, hence a map, not one name."""
    roots: dict[str, str] = {}
    for rel in known:
        if posixpath.basename(rel) != "pubspec.yaml":
            continue
        try:
            text = (config.root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _DART_PKG_NAME_RE.search(text)
        if m:
            roots[m.group(1)] = posixpath.dirname(rel)
    return roots


# --- reindex (apply changes to the DB) --------------------------------------

def _index_one(config, conn, rel, file_hash_val, known, alias_configs, stamp, src=None, dart_pkgs=None):
    """Index a single file: store its row, symbols, and dependency edges."""
    abs_path = config.root / rel
    if src is None:
        try:
            src = abs_path.read_bytes()
        except OSError:
            return
    lang = config.lang_for(rel)
    stat = abs_path.stat()
    db.upsert_file(conn, rel, lang, file_hash_val, stat.st_mtime, stat.st_size, stamp)
    symbols, imports, calls, refs = ([], [], [], [])
    if lang:
        symbols, imports, calls, refs = extract(lang, src)
    db.replace_symbols(conn, rel, symbols)
    db.replace_edges(
        conn,
        rel,
        resolve_imports(lang, imports, rel, known, alias_configs, dart_pkgs) if lang else [],
    )
    db.replace_calls(conn, rel, calls)
    db.replace_refs(conn, rel, refs)


def reindex(config: cfg.Config, conn, paths: list[str] | None = None, force: bool = False) -> dict:
    """Bring the index up to date with disk. Only changed/new/deleted files are
    touched (unless force=True, which re-extracts every file — useful after an
    extractor upgrade; summaries/notes/embeddings are preserved)."""
    disk = disk_state(config)
    known = set(disk)
    d = diff(conn, disk)
    alias_configs = tsconfig.load_alias_configs(config)
    dart_pkgs = dart_package_roots(config, known)

    targets = sorted(known) if force else d["new"] + d["changed"]
    if paths is not None:
        wanted = set(paths)
        targets = [p for p in targets if p in wanted]

    stamp = now_iso()
    for rel in targets:
        _index_one(config, conn, rel, disk[rel], known, alias_configs, stamp, dart_pkgs=dart_pkgs)

    for rel in d["deleted"]:
        db.forget_file(conn, rel)

    # Record package.json script entrypoints so orphans() doesn't flag them as
    # dead. A full walk already happened above, so this is cheap.
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('script_entrypoints',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(sorted(script_entrypoints(config, known))),),
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('last_reindex',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (stamp,),
    )
    conn.commit()
    return {
        "indexed": len(targets),
        "new": len(d["new"]),
        "changed": len(d["changed"]),
        "deleted": len(d["deleted"]),
        "total_files": len(known),
    }


def reindex_paths(config: cfg.Config, conn, rels: list[str]) -> dict:
    """Incrementally reindex specific files WITHOUT walking/hashing the whole tree.
    Used by the post-edit hook so a single save stays cheap on large monorepos.
    Edges resolve against the already-indexed file set."""
    alias_configs = tsconfig.load_alias_configs(config)
    known = set(db.stored_hashes(conn))
    known.update(rels)
    dart_pkgs = dart_package_roots(config, known)
    stamp = now_iso()
    touched = 0
    for rel in rels:
        abs_path = config.root / rel
        if not abs_path.exists():
            db.forget_file(conn, rel)
            touched += 1
            continue
        if config.is_ignored(abs_path):
            continue
        try:
            src = abs_path.read_bytes()
        except OSError:
            continue
        h = hashlib.sha1(src).hexdigest()
        prev = conn.execute("SELECT hash FROM files WHERE path=?", (rel,)).fetchone()
        if prev and prev["hash"] == h:
            continue
        _index_one(config, conn, rel, h, known, alias_configs, stamp, src=src, dart_pkgs=dart_pkgs)
        touched += 1
    # Editing a package.json can change which files are script entrypoints; refresh
    # the cached set so orphans() stays accurate without needing a full reindex.
    if any(posixpath.basename(r) == "package.json" for r in rels):
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('script_entrypoints',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(sorted(script_entrypoints(config, known))),),
        )
    conn.commit()
    return {"touched": touched, "files": len(rels)}


def _to_rel(config, arg: str) -> str | None:
    p = Path(arg)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(config.root.resolve()).as_posix()
        except ValueError:
            return None
    return posixpath.normpath(arg)


def main():  # `cerebro-index` entry point; with file args, does an incremental update
    import json
    import sys

    config = cfg.Config.load()
    conn = db.connect(config.db_path)
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if args:
        rels = [r for r in (_to_rel(config, a) for a in args) if r]
        result = reindex_paths(config, conn, rels)
        result["mode"] = "incremental"
    else:
        result = reindex(config, conn, force=force)
        result["mode"] = "full-force" if force else "full"
    result["root"] = str(config.root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
