#!/usr/bin/env python3
"""PostToolUse: record that Cerebro was used this session, so the cerebro-first
nudge (cerebro-first.py) stays quiet for the rest of the session. Best-effort."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(tempfile.gettempdir()) / "cerebro-hook-state"

try:
    data = json.load(sys.stdin)
    sid = data.get("session_id") or "nosession"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{sid}.cerebro-used").write_text("1")
except Exception:
    pass
sys.exit(0)
