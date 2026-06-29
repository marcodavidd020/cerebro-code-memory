#!/usr/bin/env python3
"""SessionStart hook: inject the Cerebro project map into a new session so the
model starts already knowing the codebase layout instead of re-exploring folders.

Locates the brain by walking up from the session's cwd to the nearest
`.cerebro/brain.db`. Best-effort: if Cerebro isn't set up here, it stays silent.
Point it at the cerebro checkout with the CEREBRO_HOME env var.
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


def read_input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def main() -> None:
    data = read_input()
    cwd = data.get("cwd") or os.getcwd()
    root = find_brain_root(cwd)
    if root is None:
        return

    # One process does git-sync + map + decisions, so the heavy imports
    # (numpy/networkx/server) load once instead of three times.
    env = {**os.environ, "CEREBRO_ROOT": str(root)}
    home = os.environ.get("CEREBRO_HOME")
    uv = os.environ.get("CEREBRO_UV", "uv")  # absolute path avoids PATH issues in hooks

    # Refresh scoped dependency graphs the project opted into via
    # `.cerebro/graphs.toml`. The reindex hook only updates the structural index,
    # never the HTML, so without this the graphs drift stale. Best-effort and
    # instant no-op when no graphs.toml exists; output is captured so it can't
    # corrupt the context JSON emitted below.
    graph_cmd = (
        [uv, "run", "--directory", home, "cerebro", "graph-all"]
        if home
        else ["cerebro", "graph-all"]
    )
    try:
        subprocess.run(graph_cmd, env=env, capture_output=True, text=True, timeout=30)
    except Exception:
        pass  # never block session start on graph rendering

    # Opt-in background summarization: when CEREBRO_AUTOSUMMARIZE=N is set, warm
    # the N most-central files that still lack a summary, each session, enriching
    # semantic search over time. OFF by default — it spends tokens via `claude -p`,
    # so the user must turn it on. Detached (Popen, no wait) so it never blocks.
    n = os.environ.get("CEREBRO_AUTOSUMMARIZE", "").strip()
    if n.isdigit() and int(n) > 0:
        summ_cmd = (
            [uv, "run", "--directory", home, "cerebro", "summarize", "--limit", n]
            if home
            else ["cerebro", "summarize", "--limit", n]
        )
        try:
            subprocess.Popen(
                summ_cmd, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass  # best-effort; missing claude CLI just means no summaries warmed

    cmd = (
        [uv, "run", "--directory", home, "cerebro", "session-context"]
        if home
        else ["cerebro", "session-context"]
    )
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=45)
        context = out.stdout.strip()
    except Exception:
        return
    if not context:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
