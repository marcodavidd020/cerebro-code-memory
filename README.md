# Cerebro 🧠

**Persistent code-knowledge memory across AI chat sessions.**

Every new chat re-analyzes your project's folders from scratch to understand it,
burning tokens re-discovering what a previous chat already learned. Cerebro caches
the *understanding* — not just the files — in a small SQLite "brain" that lives
**outside** the chat. New sessions **query** it instead of re-reading folders.

Instead of reading 50 files (~100k tokens) to understand a project, the model makes
one `cerebro_map()` call (~2-3k tokens) plus a few targeted lookups.

## How it works

Three layers of "traces", cheapest first:

1. **Structural map** (free, no LLM) — `tree-sitter` extracts symbols + imports and
   builds a dependency graph. Imports resolve both relative paths and **tsconfig /
   jsconfig path aliases** (`@/...`), so Next.js / NestJS monorepos get real edges.
   **PageRank** ranks the most important modules. Each file is hashed so changes
   are detectable. Languages: Python, JavaScript / TypeScript (incl. JSX/TSX)
   and Dart / Flutter.
2. **Cached summaries** (the big saver) — as a chat understands a file, it calls
   `cerebro_record(path, summary)` to store a 1-3 sentence **English** summary
   (English tokenizes ~15-30% cheaper than Spanish). Future sessions reuse it.
3. **Freshness** — each summary is tied to the file's hash. If the file changed,
   the trace is flagged **stale** so only that file gets re-read.

> Why not Dijkstra? Code knowledge is a *relevance* problem, not a shortest-path one.
> The useful algorithms are graph traversal (BFS/DFS, for impact) and centrality
> (PageRank, for ranking) — not weighted routing.

## MCP tools

| Tool | Purpose |
|------|---------|
| `cerebro_map(top=30)` | Cheap project overview, modules ranked by centrality. **Call first.** |
| `cerebro_get(path)` | Summary + symbols + dependencies of a file, without reading it. |
| `cerebro_search(query)` | Hybrid semantic + keyword search; semantic hits resolve to the exact symbol (`path:line`), not just the file. |
| `cerebro_record(path, summary)` | Leave a trace: store your understanding of a file. |
| `cerebro_note(content, topic?)` | Record a decision / domain rule / gotcha (the *why*). |
| `cerebro_recall(query?)` | Recall decisions recorded by past sessions. |
| `cerebro_stale()` | Files changed since last index + stale summaries. |
| `cerebro_sync()` | Catch branch switch / git pull / external edits and reindex them. |
| `cerebro_reindex(paths?)` | Refresh the structural index (only changed files). |
| `cerebro_impact(path)` | Transitive blast radius: everything that (in)directly imports a file. |
| `cerebro_cycles()` | Circular-import groups (architecture smell). |
| `cerebro_orphans(prefix?)` | Code files nothing imports — dead-code candidates (file-level). |
| `cerebro_dead_symbols(prefix?)` | Unused-export candidates: functions/classes/methods referenced nowhere *in their own project*, inside files that *are* imported (symbol-level dead code). |
| `cerebro_callers(name)` | Call sites of a symbol (who calls it, with enclosing fn + line). |
| `cerebro_calls(path)` | Internal functions a file calls (outgoing call edges). |

## Install

**One command** (published on PyPI) — add the MCP server to Claude Code:

```bash
claude mcp add cerebro -- uvx cerebro-code-memory
```

Or install the full **Claude Code plugin** (MCP server + session hooks + cerebro-first subagents):

```
/plugin marketplace add marcodavidd020/cerebro-mcp
/plugin install cerebro@cerebro
```

Requires Python ≥ 3.10. Point Cerebro at a repo with `CEREBRO_ROOT=/path/to/repo`; it
also auto-detects the nearest ancestor `.cerebro/` brain (handy in monorepos).

## Quick start

One command onboards any repo — it indexes and prints the exact registration line:

```bash
uv tool install --from . cerebro          # installs the `cerebro` command globally (dev)
cd /path/to/your/repo
cerebro setup --summarize --embed          # index (+ warm summaries / semantic index), then prints next steps
```

`cerebro setup` is idempotent. Then run the `claude mcp add …` line it prints, reload
your editor, and the `cerebro_*` tools are available in chat.

## Unified CLI

```
cerebro                 # no args -> MCP server (stdio); this is what the registration runs
cerebro setup           # index this repo + print MCP registration
cerebro index [--force] # build/refresh the index
cerebro search <query>  # hybrid semantic + keyword search
cerebro map             # project overview
cerebro graph           # interactive dependency-graph HTML
cerebro obsidian        # export an Obsidian vault
cerebro summarize / embed
cerebro impact / cycles / orphans / callers / calls / recall
cerebro doc-audit <vault>   # living docs: flag knowledge notes whose referenced code changed
```

### Living documentation (`doc-audit`)

`cerebro doc-audit <markdown-vault>` cross-checks a curated knowledge vault against
the code index: it parses each note's code references (`path:line`, backticked
symbols) and the note's `ultima_verificacion`/`fecha`, then flags notes whose
referenced files **changed after** they were verified, **moved/were deleted**, or
mention a **symbol that no longer exists**. `--aliases` maps wiki app names to repo
dirs (`backend_app=fenix-store-backend,…`); `--fix` patches stale notes' frontmatter
to `estado: revisar`. This is the bridge between an auto-fresh code index and a
human-curated wiki — documentation that can't silently rot.

`cerebro doc-refresh <note>` closes the loop: it re-audits one stale note against
the *live* code and prints a briefing — current symbols, summary and dependents for
each reference, plus the new location of any moved file — exactly the context an
agent needs to propose the update (self-healing docs, human-reviewed).

Without a global install, prefix any command with `uv run` (e.g. `uv run cerebro setup`).

Point Cerebro at a specific repo with `CEREBRO_ROOT=/path/to/repo`. It honors
`.gitignore` plus an optional **`.cerebroignore`** (same syntax) for excluding
heavy non-source dirs (`backup/`, `**/uploads/`, …) without touching your VCS
config. `node_modules/`, `.next/`, `dist/`, `build/` are ignored by default.

Works on monorepos: index the whole thing at once (a single brain at the root,
with cross-package alias resolution) or per sub-app (`CEREBRO_ROOT` per package).

### Register with Claude Code

```bash
claude mcp add cerebro -- uv --directory /path/to/cerebro run cerebro
```

Set `CEREBRO_ROOT` to the project you want the brain to cover. See `plugin/` for the
optional Claude Code plugin that auto-injects the map at session start and flags
edited files as stale.

## Scope (MVP) 

In: structural map, cached summaries, freshness, keyword search, tsconfig/jsconfig
alias resolution, `.cerebroignore`, batch summary warming (`cerebro-summarize`, via
headless `claude -p` — no API key), a decision log (`cerebro_note` /
`cerebro_recall`, surfaced at session start), and git-aware freshness
(`cerebro_sync` catches branch switch / pull / external edits across nested repos),
and optional local semantic search (`cerebro-embed` + `--extra semantic`: model2vec
embeddings — one vector per symbol — no torch, no API key, nothing leaves the
machine — `cerebro_search` becomes hybrid semantic + keyword and lands on the exact
symbol (`path:line`), not just the file), and visualizations (`cerebro-graph` →
self-contained interactive HTML dependency graph; `cerebro-export-obsidian` → an
Obsidian vault where imports are `[[links]]`), and architecture insights
(`cerebro_impact` transitive blast radius, `cerebro_cycles` circular imports,
`cerebro_orphans` dead-code candidates), and a symbol-level call graph
(`cerebro_callers` / `cerebro_calls`, tree-sitter name-resolved).
Deferred to v2: a live file watcher, and LSP-backed call graph for type-precise
resolution (the current call graph resolves by name).

## License

MIT © 2026 Marco Toledo — see [LICENSE](LICENSE).
