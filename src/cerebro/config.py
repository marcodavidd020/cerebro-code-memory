"""Project configuration: repo root discovery, ignore rules, file walking.

The "root" is the directory whose code Cerebro indexes. Resolution order:
  1. CEREBRO_ROOT env var (explicit override)
  2. the nearest ancestor holding a built brain (.cerebro/brain.db) — so a
     polyrepo subfolder (each its own git repo) resolves to the shared root
     brain instead of re-indexing itself. Mirrors the SessionStart hook.
  3. the enclosing git toplevel (if inside a git repo)
  4. the current working directory
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pathspec

# Directories / globs we never index, regardless of .gitignore.
DEFAULT_IGNORES = [
    ".git/",
    ".cerebro/",
    ".cerebroignore",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".dart_tool/",
    "dist/",
    "build/",
    ".next/",
    ".cache/",
    "*.pyc",
    "*.lock",
    "*.min.js",
    "*.map",
    ".DS_Store",
    # Secrets / local env — never index (privacy invariant: nothing leaves the
    # machine). Nested sub-repos in a polyrepo .gitignore these, but Cerebro only
    # reads the root .gitignore, so make them a hard default.
    ".env",
    ".env.*",
]

# File extension -> tree-sitter-language-pack language name.
LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".dart": "dart",
}


def find_root(start: str | None = None) -> Path:
    env = os.environ.get("CEREBRO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    start_path = Path(start or os.getcwd()).resolve()
    # Walk up to an existing brain first: in a polyrepo each subfolder is its own
    # git repo, so the git step below would resolve to the subfolder and miss the
    # shared root brain. Finding `.cerebro/brain.db` upward makes one brain at the
    # monorepo root serve every nested package automatically — no per-folder
    # CEREBRO_ROOT needed. (Only matches a *built* brain, so first-time `setup`
    # still falls through to git below.)
    probe = start_path if start_path.is_dir() else start_path.parent
    for d in (probe, *probe.parents):
        if (d / ".cerebro" / "brain.db").exists():
            return d
    try:
        out = subprocess.run(
            ["git", "-C", str(start_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except Exception:
        pass
    return start_path


@dataclass
class Config:
    root: Path
    db_path: Path
    spec: pathspec.PathSpec

    @classmethod
    def load(cls, start: str | None = None) -> "Config":
        root = find_root(start)
        db_path = root / ".cerebro" / "brain.db"
        patterns = list(DEFAULT_IGNORES)
        # .gitignore + an optional .cerebroignore (same gitignore syntax) let the
        # user exclude heavy non-source dirs (backups, uploads) without polluting
        # their VCS config.
        for fname in (".gitignore", ".cerebroignore"):
            f = root / fname
            if f.exists():
                patterns += f.read_text(encoding="utf-8", errors="ignore").splitlines()
        spec = pathspec.PathSpec.from_lines("gitignore", patterns)
        return cls(root=root, db_path=db_path, spec=spec)

    def is_ignored(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return True
        if path.is_dir():
            rel += "/"
        return self.spec.match_file(rel)

    def iter_files(self):
        """Yield (relative_posix_path, absolute_Path) for every indexable file."""
        for dirpath, dirnames, filenames in os.walk(self.root):
            d = Path(dirpath)
            # Prune ignored directories in place so os.walk never descends them.
            dirnames[:] = sorted(
                dn for dn in dirnames if not self.is_ignored(d / dn)
            )
            for fn in sorted(filenames):
                fp = d / fn
                if self.is_ignored(fp):
                    continue
                yield fp.relative_to(self.root).as_posix(), fp

    @staticmethod
    def lang_for(rel: str) -> str | None:
        return LANG_BY_EXT.get(Path(rel).suffix.lower())
