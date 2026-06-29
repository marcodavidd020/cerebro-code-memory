"""Git-aware freshness.

The post-edit hook only catches edits made *through* Claude Code. Branch switches,
`git pull`, rebases, and edits in the raw editor go unnoticed until a manual reindex.
This diffs git state since the last sync and incrementally reindexes only the changed
files. It handles a single repo at the root OR several nested repos (e.g. a folder of
sub-app repos, like a Fenix-style multi-repo workspace).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import indexer


def _git(repo_abs: Path, *args: str, timeout: int = 15) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_abs), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def find_git_repos(config, max_depth: int = 3) -> list[str]:
    """Repo dirs relative to root. Returns [''] when the root itself is a repo."""
    root = config.root
    if (root / ".git").exists():
        return [""]
    repos: list[str] = []
    for dirpath, dirnames, _files in os.walk(root):
        d = Path(dirpath)
        depth = len(d.relative_to(root).parts)
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [dn for dn in dirnames if not config.is_ignored(d / dn)]
        if (d / ".git").exists():
            repos.append(d.relative_to(root).as_posix())
            dirnames[:] = []  # a repo is one unit — don't descend for nested repos
    return repos


def _porcelain_paths(status: str) -> set[str]:
    paths = set()
    for line in status.splitlines():
        path = line[3:]
        if " -> " in path:  # rename: "old -> new"
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            paths.add(path)
    return paths


def sync(config, conn) -> dict:
    """Reindex files changed via git since the last sync, across all repos under
    the root. First run just records each repo's HEAD as a baseline."""
    repos = find_git_repos(config)
    if not repos:
        return {"git": False, "changed": 0}

    changed_root_rel: set[str] = set()
    new_heads: dict[str, str] = {}
    for repo_rel in repos:
        repo_abs = config.root / repo_rel if repo_rel else config.root
        head = _git(repo_abs, "rev-parse", "HEAD")
        if head is None:
            continue
        key = f"git_head:{repo_rel}"
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        old = row["value"] if row else None

        files: set[str] = set()
        if old and old != head:
            diff = _git(repo_abs, "diff", "--name-only", old, head)
            if diff:
                files.update(diff.splitlines())
        status = _git(repo_abs, "status", "--porcelain")
        if status:
            files.update(_porcelain_paths(status))

        for f in files:
            rel = f"{repo_rel}/{f}" if repo_rel else f
            if not config.is_ignored(config.root / rel):
                changed_root_rel.add(rel)
        new_heads[key] = head

    rels = sorted(changed_root_rel)
    if rels:
        indexer.reindex_paths(config, conn, rels)
    for key, head in new_heads.items():
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, head),
        )
    conn.commit()
    return {"git": True, "repos": len(new_heads), "changed": len(rels)}


def main():  # `cerebro-sync` entry point
    import json

    from . import config as cfg
    from . import db

    config = cfg.Config.load()
    conn = db.connect(config.db_path)
    result = sync(config, conn)
    result["root"] = str(config.root)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
