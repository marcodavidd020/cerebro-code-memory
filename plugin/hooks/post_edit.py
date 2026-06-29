#!/usr/bin/env python3
"""PostToolUse hook (Edit|Write|MultiEdit): keep the structural index fresh.

Reindexes ONLY the edited file (incremental, no full-tree walk), so a save stays
cheap even on large monorepos. Locates the brain by walking up from the edited
file to the nearest `.cerebro/brain.db`, so it updates the right index regardless
of cwd. Best-effort and silent on failure: set CEREBRO_HOME to the cerebro checkout.
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def find_brain_root(start: str):
    p = Path(start).resolve()
    p = p if p.is_dir() else p.parent
    for d in (p, *p.parents):
        if (d / ".cerebro" / "brain.db").exists():
            return d
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tool_input = data.get("tool_input") or {}
    fpath = tool_input.get("file_path")
    if not fpath:
        return
    root = find_brain_root(fpath)
    if root is None:
        return  # this project isn't tracked by Cerebro

    env = {**os.environ, "CEREBRO_ROOT": str(root)}
    home = os.environ.get("CEREBRO_HOME")
    uv = os.environ.get("CEREBRO_UV", "uv")  # absolute path avoids PATH issues in hooks
    base = [uv, "run", "--directory", home, "cerebro-index"] if home else ["cerebro-index"]
    try:
        subprocess.run(base + [fpath], env=env, capture_output=True, text=True, timeout=30)
    except Exception:
        pass  # never block an edit on indexing


if __name__ == "__main__":
    main()
