"""tsconfig / jsconfig path-alias resolution.

Next.js and NestJS projects import via aliases like `@/components/Button` instead
of relative paths. Those are declared in `compilerOptions.paths` (relative to
`baseUrl`). Without expanding them, the dependency graph misses most edges in
alias-heavy frontends. This module parses those configs (tolerating JSONC and a
single level of `extends`) and expands an aliased import to candidate repo-relative
module paths. The indexer then resolves a candidate to a real file.
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AliasConfig:
    dir: str                       # posix dir of the config, relative to root ("" == root)
    base_url: str                  # posix dir that `paths` resolve from, relative to root
    patterns: dict[str, list[str]] # e.g. {"@/*": ["./*"]}


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments while respecting string literals, so glob
    patterns like "@/*" and "**/*.ts" (which contain /* and */) are not mistaken
    for comment delimiters."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\n\r":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _loads_jsonc(text: str):
    text = _strip_comments(text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)  # trailing commas
    return json.loads(text)


def _resolve_extends(base_dir: Path, ext: str) -> Path | None:
    # Only follow path-like extends (./base, ../tsconfig.base.json); skip packages.
    if not (ext.startswith(".") or "/" in ext):
        return None
    cand = base_dir / ext
    if cand.suffix != ".json":
        cand = base_dir / (ext + ".json")
    return cand if cand.exists() else None


def _read_with_extends(abs_path: Path, seen: set) -> dict:
    rp = abs_path.resolve()
    if rp in seen:
        return {}
    seen.add(rp)
    try:
        data = _loads_jsonc(abs_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    co = data.get("compilerOptions") or {}
    result = {"baseUrl": co.get("baseUrl"), "paths": co.get("paths")}
    ext = data.get("extends")
    if isinstance(ext, str) and (result["baseUrl"] is None or result["paths"] is None):
        parent = _resolve_extends(abs_path.parent, ext)
        if parent is not None:
            pdata = _read_with_extends(parent, seen)
            for key in ("baseUrl", "paths"):
                if result[key] is None:
                    result[key] = pdata.get(key)
    return result


def load_alias_configs(config) -> list[AliasConfig]:
    """Scan the repo for tsconfig.json / jsconfig.json files that declare paths."""
    out: list[AliasConfig] = []
    for rel, abs_path in config.iter_files():
        if posixpath.basename(rel) not in ("tsconfig.json", "jsconfig.json"):
            continue
        merged = _read_with_extends(abs_path, set())
        raw_paths = merged.get("paths") or {}
        if not raw_paths:
            continue
        patterns = {
            k: (v if isinstance(v, list) else [v]) for k, v in raw_paths.items()
        }
        cfg_dir = posixpath.dirname(rel)
        base_url = merged.get("baseUrl") or "."
        base = posixpath.normpath(posixpath.join(cfg_dir, base_url))
        out.append(AliasConfig(dir=cfg_dir, base_url=base, patterns=patterns))
    return out


def _nearest(configs: list[AliasConfig], importer_rel: str) -> AliasConfig | None:
    best = None
    for c in configs:
        prefix = (c.dir + "/") if c.dir else ""
        if importer_rel.startswith(prefix):
            if best is None or len(c.dir) > len(best.dir):
                best = c
    return best


def _match(pattern: str, name: str) -> str | None:
    """Return the wildcard capture if `name` matches `pattern`, else None.
    Exact (no-`*`) patterns return '' on an exact match."""
    if "*" in pattern:
        pre, post = pattern.split("*", 1)
        if name.startswith(pre) and name.endswith(post) and len(name) >= len(pre) + len(post):
            return name[len(pre): len(name) - len(post)] if post else name[len(pre):]
        return None
    return "" if name == pattern else None


def expand(import_str: str, importer_rel: str, configs: list[AliasConfig]) -> list[str]:
    """Expand an aliased import to candidate repo-relative module paths (no
    extension). The indexer resolves these against the known file set."""
    cfg = _nearest(configs, importer_rel)
    if cfg is None:
        return []
    out: list[str] = []
    for pattern, targets in cfg.patterns.items():
        cap = _match(pattern, import_str)
        if cap is None:
            continue
        for t in targets:
            sub = t.replace("*", cap, 1) if "*" in t else t
            out.append(posixpath.normpath(posixpath.join(cfg.base_url, sub)))
    return out
