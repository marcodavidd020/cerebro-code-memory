#!/usr/bin/env python3
"""SessionEnd hook: auto-record.

When CEREBRO_AUTORECORD=N (a positive int) is set, re-summarize up to N files whose
cached summary went STALE during the session — so the NEXT session reuses fresh
traces instead of re-reading the files you changed. Detached (never blocks exit)
and OFF by default (it spends a little via `claude -p`; set the env var to opt in).
Mirrors the spawn pattern of session_start.py's autosummarize.
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
    n = os.environ.get("CEREBRO_AUTORECORD", "").strip()
    if not (n.isdigit() and int(n) > 0):
        return  # opt-in: set CEREBRO_AUTORECORD=N to enable

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    root = find_brain_root(cwd)
    if root is None:
        return  # not a Cerebro project

    env = {**os.environ, "CEREBRO_ROOT": str(root)}
    home = os.environ.get("CEREBRO_HOME")
    uv = os.environ.get("CEREBRO_UV", "uv")
    cmd = (
        [uv, "run", "--directory", home, "cerebro", "summarize", "--stale", "--limit", n]
        if home
        else ["cerebro", "summarize", "--stale", "--limit", n]
    )
    try:
        subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass  # best-effort; never block session exit


if __name__ == "__main__":
    main()
