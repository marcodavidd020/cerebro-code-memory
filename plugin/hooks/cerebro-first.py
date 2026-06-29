#!/usr/bin/env python3
"""PreToolUse nudge: enforce 'cerebro-first' the deterministic way.

In a project that HAS a Cerebro brain, if the model reaches for raw code search
(grep/rg/find, or the native Grep/Glob tools) before consulting Cerebro, block the
call ONCE per session with a reminder to use cerebro_search / cerebro_get first
(far cheaper in tokens). Strictly bounded:
  - only where a `.cerebro/brain.db` exists upward from cwd,
  - at most ONE nudge per session,
  - never nudges once Cerebro has been used this session,
  - the command is never permanently blocked: re-running it passes.

Best-effort: any error → allow silently (a guardrail you trust beats one that
gets in the way).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(tempfile.gettempdir()) / "cerebro-hook-state"

# Raw code-search tools Cerebro can usually answer cheaper. Kept narrow on purpose
# (no cat/ls/head — too generic) to avoid noisy false nudges.
_EXPLORE = re.compile(r"\b(grep|rg|find|ack|ag)\b")


def find_brain(start: str):
    try:
        p = Path(start).resolve()
    except Exception:
        return None
    p = p if p.is_dir() else p.parent
    for d in (p, *p.parents):
        if (d / ".cerebro" / "brain.db").exists():
            return d
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    sid = data.get("session_id") or "nosession"
    cwd = data.get("cwd") or os.getcwd()

    # Only act where Cerebro is actually set up.
    if find_brain(cwd) is None:
        sys.exit(0)

    # Is this raw exploration?
    if tool in ("Grep", "Glob"):
        exploratory = True
    elif tool == "Bash":
        exploratory = bool(_EXPLORE.search(ti.get("command", "") or ""))
    else:
        exploratory = False
    if not exploratory:
        sys.exit(0)

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        sys.exit(0)
    used = STATE_DIR / f"{sid}.cerebro-used"
    nudged = STATE_DIR / f"{sid}.nudged"

    # Already used Cerebro, or already nudged once → let it through.
    if used.exists() or nudged.exists():
        sys.exit(0)

    try:
        nudged.write_text("1")
    except Exception:
        pass

    sys.stderr.write(
        "💡 This project has a Cerebro brain — querying it is far cheaper than raw "
        "search.\nBefore grep/find, try:\n"
        "  • cerebro_search(\"<what you're looking for>\")  → locates code at path:line\n"
        "  • cerebro_get(\"<path>\")  → a file's summary + symbols + deps, without reading it\n"
        "One-time reminder. If Cerebro doesn't have what you need, just re-run your command.\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
