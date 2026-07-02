"""SQLite storage: schema, connection, and low-level write/query helpers.

A single file, `.cerebro/brain.db`, holds every "trace": the structural index
(files, symbols, edges), the cached English summaries, the decision notes
(reserved for v2), and an FTS5 index used by keyword search.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    lang        TEXT,
    hash        TEXT NOT NULL,
    mtime       REAL,
    size        INTEGER,
    indexed_at  TEXT NOT NULL,
    -- Fingerprint of the file's *shape* (symbol signatures + import specifiers),
    -- not its bytes. Drives summary-staleness: a summary describes a file's ROLE,
    -- which changes with structure, not with comments/whitespace/function bodies.
    struct_hash TEXT
);

CREATE TABLE IF NOT EXISTS symbols (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    line       INTEGER,
    signature  TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

CREATE TABLE IF NOT EXISTS edges (
    src_path  TEXT NOT NULL,
    dst_path  TEXT NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'import',
    PRIMARY KEY (src_path, dst_path, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_path);

-- Symbol-level call graph (tree-sitter, name-resolved): src_symbol in src_path
-- calls/references a symbol named dst_name. src_symbol is NULL at module scope.
CREATE TABLE IF NOT EXISTS calls (
    src_path   TEXT NOT NULL,
    src_symbol TEXT,
    dst_name   TEXT NOT NULL,
    line       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_calls_dst ON calls(dst_name);
CREATE INDEX IF NOT EXISTS idx_calls_src ON calls(src_path);

-- Identifier references: a distinct (path, name) for every name USED in a file
-- (calls, JSX, type annotations, value reads), EXCLUDING the file's own
-- definition sites. Lets dead_symbols() ask "is this symbol's name referenced
-- anywhere?" — a name absent from refs entirely is an unused-export candidate.
CREATE TABLE IF NOT EXISTS refs (
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (path, name)
);
CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);

CREATE TABLE IF NOT EXISTS summaries (
    path        TEXT PRIMARY KEY,
    summary_en  TEXT NOT NULL,
    model       TEXT,
    source_hash TEXT,
    -- The file's struct_hash when this summary was written. Compared against
    -- files.struct_hash to flag staleness; source_hash (bytes) is kept as a
    -- latent fallback for brains written before struct_hash existed.
    struct_hash TEXT,
    updated_at  TEXT NOT NULL
);

-- Reserved for v2 (decision log). Created now so the schema is stable.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Optional semantic layer (legacy, file-level): one vector per file. Superseded
-- by symbol_embeddings below; kept so older brains still open cleanly.
CREATE TABLE IF NOT EXISTS embeddings (
    path       TEXT PRIMARY KEY,
    dim        INTEGER,
    vec        BLOB NOT NULL,
    doc_hash   TEXT,
    updated_at TEXT NOT NULL
);

-- Symbol-level semantic vectors: one row per symbol (function/class), plus one
-- whole-file row (name IS NULL) for files with no indexable symbols. Lets search
-- land on the exact symbol + line, not just the file. doc_hash is a per-file
-- fingerprint (identical on every row of a file) so build() skips unchanged files.
CREATE TABLE IF NOT EXISTS symbol_embeddings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL,
    name       TEXT,
    line       INTEGER,
    kind       TEXT,
    dim        INTEGER,
    vec        BLOB NOT NULL,
    doc_hash   TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symemb_path ON symbol_embeddings(path);

-- One row per (path, kind). kind='symbol' aggregates a file's symbol names;
-- kind='summary' holds the file's English summary. Maintained manually.
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    path UNINDEXED,
    kind UNINDEXED,
    text
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Set busy_timeout FIRST: switching to WAL itself takes a write lock, so a
    # concurrent connection (e.g. the post-edit hook's reindex while the MCP
    # server connects) must wait here rather than fail with "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn) -> None:
    """Additive, idempotent migration for brains created before a column existed.
    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so it never adds
    a column to an already-created brain — this ALTERs in what's missing. Old rows
    get the column as NULL, and every staleness path falls back to source_hash
    until the file is reindexed / re-summarized, so existing brains keep working."""
    for table, col, decl in (
        ("files", "struct_hash", "TEXT"),
        ("summaries", "struct_hash", "TEXT"),
    ):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# --- structural index writes -------------------------------------------------

def upsert_file(conn, path, lang, file_hash, mtime, size, indexed_at, struct_hash=None):
    conn.execute(
        """INSERT INTO files(path, lang, hash, mtime, size, indexed_at, struct_hash)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             lang=excluded.lang, hash=excluded.hash, mtime=excluded.mtime,
             size=excluded.size, indexed_at=excluded.indexed_at,
             struct_hash=excluded.struct_hash""",
        (path, lang, file_hash, mtime, size, indexed_at, struct_hash),
    )


def replace_symbols(conn, path, symbols):
    """symbols: iterable of (kind, name, line, signature)."""
    conn.execute("DELETE FROM symbols WHERE file_path=?", (path,))
    conn.executemany(
        "INSERT INTO symbols(file_path, kind, name, line, signature) VALUES(?,?,?,?,?)",
        [(path, k, n, ln, sig) for (k, n, ln, sig) in symbols],
    )
    names = " ".join(sorted({n for (_, n, _, _) in symbols}))
    conn.execute("DELETE FROM fts WHERE path=? AND kind='symbol'", (path,))
    if names:
        conn.execute(
            "INSERT INTO fts(path, kind, text) VALUES(?, 'symbol', ?)", (path, names)
        )


def replace_edges(conn, src_path, edges):
    """edges: an iterable of dst paths (all recorded as kind 'import'), or a
    mapping {dst_path: kind} to record edge kinds (e.g. type-only TS imports)."""
    items = edges.items() if isinstance(edges, dict) else ((d, "import") for d in edges)
    rows = sorted({(src_path, d, k) for d, k in items})
    conn.execute("DELETE FROM edges WHERE src_path=?", (src_path,))
    conn.executemany(
        "INSERT OR IGNORE INTO edges(src_path, dst_path, kind) VALUES(?,?,?)",
        rows,
    )


def replace_calls(conn, src_path, calls):
    """calls: iterable of (src_symbol|None, dst_name, line)."""
    conn.execute("DELETE FROM calls WHERE src_path=?", (src_path,))
    conn.executemany(
        "INSERT INTO calls(src_path, src_symbol, dst_name, line) VALUES(?,?,?,?)",
        [(src_path, sym, name, line) for (sym, name, line) in calls],
    )


def replace_refs(conn, src_path, names):
    """names: iterable of identifier names used (referenced) in src_path."""
    conn.execute("DELETE FROM refs WHERE path=?", (src_path,))
    conn.executemany(
        "INSERT OR IGNORE INTO refs(path, name) VALUES(?,?)",
        [(src_path, n) for n in sorted(set(names))],
    )


def forget_file(conn, path):
    """Remove every trace of a file that no longer exists on disk."""
    conn.execute("DELETE FROM files WHERE path=?", (path,))
    conn.execute("DELETE FROM symbols WHERE file_path=?", (path,))
    conn.execute("DELETE FROM edges WHERE src_path=? OR dst_path=?", (path, path))
    conn.execute("DELETE FROM calls WHERE src_path=?", (path,))
    conn.execute("DELETE FROM refs WHERE path=?", (path,))
    conn.execute("DELETE FROM summaries WHERE path=?", (path,))
    conn.execute("DELETE FROM embeddings WHERE path=?", (path,))
    conn.execute("DELETE FROM symbol_embeddings WHERE path=?", (path,))
    conn.execute("DELETE FROM fts WHERE path=?", (path,))


# --- reads -------------------------------------------------------------------

def stored_hashes(conn) -> dict[str, str]:
    return {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM files")}


def symbols_for(conn, path):
    return conn.execute(
        "SELECT kind, name, line, signature FROM symbols WHERE file_path=? ORDER BY line",
        (path,),
    ).fetchall()


def lang_counts(conn):
    return conn.execute(
        "SELECT COALESCE(lang,'other') AS lang, COUNT(*) AS n "
        "FROM files GROUP BY lang ORDER BY n DESC"
    ).fetchall()


def search(conn, query: str, limit: int = 15):
    """Keyword search over symbol names + summaries. Falls back to LIKE if the
    FTS5 MATCH syntax rejects the raw query."""
    try:
        rows = conn.execute(
            # summaries are higher-signal than symbol-name matches → rank them first
            "SELECT path, kind, snippet(fts, 2, '[', ']', '…', 12) AS snip "
            "FROM fts WHERE fts MATCH ? AND kind != 'note' "
            "ORDER BY (kind != 'summary'), rank LIMIT ?",
            (query, limit),
        ).fetchall()
        if rows:
            return rows
    except sqlite3.OperationalError:
        pass
    like = f"%{query}%"
    return conn.execute(
        "SELECT path, kind, substr(text,1,120) AS snip FROM fts "
        "WHERE text LIKE ? AND kind != 'note' LIMIT ?",
        (like, limit),
    ).fetchall()
