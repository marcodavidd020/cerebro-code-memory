"""Optional semantic search layer.

Keyword search (FTS5) misses intent — "where do we validate stock during purchase?"
shares no keywords with the checkout service. This embeds one vector per SYMBOL
(function/class: `path kind name signature` + the file summary), plus a whole-file
vector for files with no indexable symbols, with a small LOCAL model (model2vec, no
torch, no API key, nothing leaves the machine) and ranks by cosine similarity in
numpy — so a hit lands on the exact symbol + line, not just the file.

It is fully optional: install with `uv sync --extra semantic`. Without the extra,
every function degrades to a no-op and search stays keyword-only.
"""
from __future__ import annotations

import hashlib
import logging
import os

from . import db
from . import summaries

# Keep the local model quiet — no progress bars / HTTP chatter (set before any
# huggingface import, which model2vec does lazily inside _model()).
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
logging.getLogger("httpx").setLevel(logging.WARNING)

try:  # optional dependencies — guarded so the module imports either way
    import numpy as np
except Exception:  # pragma: no cover
    np = None

_MODEL = None
_MODEL_NAME = "minishlab/potion-base-8M"


def model_available() -> bool:
    if np is None:
        return False
    try:
        import model2vec  # noqa: F401
        return True
    except Exception:
        return False


def _model():
    global _MODEL
    if _MODEL is None:
        from model2vec import StaticModel
        _MODEL = StaticModel.from_pretrained(_MODEL_NAME)
    return _MODEL


def _docs_for(conn, path: str):
    """One document per symbol — `path kind name signature` + the file summary for
    context (there are no per-symbol summaries). A file with no indexable symbols
    gets a single whole-file document (name/line/kind None) so it stays searchable.
    Returns a list of (name, line, kind, doc)."""
    row = conn.execute(
        "SELECT summary_en FROM summaries WHERE path=?", (path,)
    ).fetchone()
    summary = row["summary_en"] if row else ""
    syms = db.symbols_for(conn, path)
    if syms:
        return [
            (
                s["name"],
                s["line"],
                s["kind"],
                f"{path} {s['kind']} {s['name']} {s['signature'] or s['name']}\n{summary}".strip(),
            )
            for s in syms
        ]
    return [(None, None, None, f"{path}\n{summary}".strip())]


def has_index(conn) -> bool:
    if np is None:
        return False
    return (
        conn.execute("SELECT COUNT(*) AS n FROM symbol_embeddings").fetchone()["n"]
        > 0
    )


def build(config, conn, only_missing: bool = True) -> dict:
    """Embed each file's symbols (one vector per symbol, plus a whole-file vector
    for symbol-less files). doc_hash is a per-file fingerprint — identical across a
    file's rows — so an unchanged file is skipped wholesale and a changed one has
    all its rows replaced atomically. `embedded` counts files re-embedded."""
    if not model_available():
        return {"ok": False, "reason": "semantic extra not installed (uv sync --extra semantic)"}
    model = _model()
    files = [
        r["path"]
        for r in conn.execute("SELECT path FROM files WHERE lang IS NOT NULL")
    ]
    have: dict[str, str] = {}
    for r in conn.execute("SELECT path, doc_hash FROM symbol_embeddings"):
        have.setdefault(r["path"], r["doc_hash"])

    targets, docs = [], []  # targets: (path, name, line, kind, fhash)
    for p in files:
        file_docs = _docs_for(conn, p)
        fhash = hashlib.sha1(
            "\x00".join(d for (_, _, _, d) in file_docs).encode("utf-8")
        ).hexdigest()
        if only_missing and have.get(p) == fhash:
            continue
        # Changed (or new) file: drop its stale rows, re-embed all of them.
        conn.execute("DELETE FROM symbol_embeddings WHERE path=?", (p,))
        for (name, line, kind, doc) in file_docs:
            targets.append((p, name, line, kind, fhash))
            docs.append(doc)
    if not docs:
        conn.commit()
        return {"ok": True, "embedded": 0, "total": len(files)}

    vecs = np.asarray(model.encode(docs), dtype="float32")
    dim = int(vecs.shape[1])
    now = summaries.now_iso()
    for (p, name, line, kind, fhash), v in zip(targets, vecs):
        conn.execute(
            "INSERT INTO symbol_embeddings"
            "(path, name, line, kind, dim, vec, doc_hash, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (p, name, line, kind, dim, v.tobytes(), fhash, now),
        )
    conn.commit()
    return {
        "ok": True,
        "embedded": len({t[0] for t in targets}),
        "total": len(files),
    }


def search(config, conn, query: str, limit: int = 10):
    """Return [(path, name, line, cosine_score), ...] best-first — name/line are
    None for a whole-file hit. [] if unavailable."""
    if not model_available() or not has_index(conn):
        return []
    rows = conn.execute(
        "SELECT path, name, line, vec FROM symbol_embeddings"
    ).fetchall()
    if not rows:
        return []
    q = np.asarray(_model().encode([query])[0], dtype="float32")
    q /= np.linalg.norm(q) + 1e-9
    mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype="float32").reshape(
        len(rows), -1
    )
    mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    sims = mat @ q
    order = np.argsort(-sims)[:limit]
    return [
        (rows[i]["path"], rows[i]["name"], rows[i]["line"], float(sims[i]))
        for i in order
    ]


def main():  # `cerebro-embed` entry point
    import json

    from . import config as cfg

    config = cfg.Config.load()
    conn = db.connect(config.db_path)
    result = build(config, conn)
    result["root"] = str(config.root)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
