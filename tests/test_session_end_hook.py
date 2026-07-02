"""Tests for the SessionEnd auto-record hook's pure logic.

The hook is a standalone script (copied to ~/.claude/hooks), so it's imported by
path. What matters is that it re-summarizes stale files in EVERY brain the session
touched — the cwd's brain PLUS the brain nearest each edited file — since post_edit
reindexes edits into per-file brains, not the cwd's.
"""
import importlib.util
import json
from pathlib import Path

HOOK_PATH = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "cerebro-session-end.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("cerebro_session_end", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_brain(root: Path) -> Path:
    """A minimal Cerebro project: a dir with .cerebro/brain.db present."""
    (root / ".cerebro").mkdir(parents=True, exist_ok=True)
    (root / ".cerebro" / "brain.db").write_text("")
    return root


def write_transcript(path: Path, edits) -> Path:
    """A JSONL transcript. `edits` is a list of (tool_name, file_path). Interleaves
    a non-tool line, a Read (must be ignored), and a malformed line to prove the
    parser is defensive."""
    lines = ['{"type":"user","message":{"role":"user","content":"hi"}}']
    lines.append(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/nope/ignored.ts"}}
        ]},
    }))
    for name, fp in edits:
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": name, "input": {"file_path": fp}}
            ]},
        }))
    lines.append("{ this is not valid json")
    path.write_text("\n".join(lines))
    return path


def test_edited_files_extracts_edits_ignoring_reads(tmp_path):
    hook = load_hook()
    t = write_transcript(tmp_path / "t.jsonl", [
        ("Edit", "/repo/a.ts"),
        ("Write", "/repo/b.ts"),
        ("MultiEdit", "/repo/c.ts"),
    ])
    assert hook.edited_files(str(t)) == ["/repo/a.ts", "/repo/b.ts", "/repo/c.ts"]


def test_edited_files_missing_transcript_is_empty(tmp_path):
    hook = load_hook()
    assert hook.edited_files(str(tmp_path / "does-not-exist.jsonl")) == []


def test_touched_roots_unions_cwd_and_edited_file_brains(tmp_path):
    hook = load_hook()
    repo_a = make_brain(tmp_path / "repoA")
    repo_b = make_brain(tmp_path / "repoB")
    t = write_transcript(tmp_path / "t.jsonl", [("Edit", str(repo_b / "src" / "x.ts"))])

    roots = hook.touched_roots(str(repo_a), str(t))

    assert roots == [repo_a.resolve(), repo_b.resolve()]  # cwd brain first, then edited-file brain


def test_touched_roots_dedupes_when_edit_in_cwd_repo(tmp_path):
    hook = load_hook()
    repo_a = make_brain(tmp_path / "repoA")
    repo_b = make_brain(tmp_path / "repoB")
    t = write_transcript(tmp_path / "t.jsonl", [
        ("Edit", str(repo_a / "in_cwd.ts")),   # same brain as cwd -> deduped
        ("Write", str(repo_b / "in_b.ts")),
    ])

    roots = hook.touched_roots(str(repo_a), str(t))

    assert roots == [repo_a.resolve(), repo_b.resolve()]


def test_touched_roots_no_transcript_is_cwd_only(tmp_path):
    hook = load_hook()
    repo_a = make_brain(tmp_path / "repoA")
    assert hook.touched_roots(str(repo_a), None) == [repo_a.resolve()]


def test_touched_roots_no_brain_anywhere_is_empty(tmp_path):
    hook = load_hook()
    # cwd has no .cerebro, transcript edits a file with no brain above it either
    t = write_transcript(tmp_path / "t.jsonl", [("Edit", str(tmp_path / "loose" / "x.ts"))])
    assert hook.touched_roots(str(tmp_path / "nowhere"), str(t)) == []
