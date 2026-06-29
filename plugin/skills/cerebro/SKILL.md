---
name: cerebro
description: Use the Cerebro brain to understand a codebase cheaply instead of re-reading folders. Invoke when starting work on a project, when you need to understand what a file does or where something lives, or before exploring directories. Query cerebro_map / cerebro_get / cerebro_search first; record what you learn with cerebro_record so future sessions reuse it.
---

# Cerebro — reuse cached code understanding

Cerebro is an MCP server that persists what previous sessions learned about this
codebase in a local SQLite brain. Querying it costs a fraction of the tokens that
re-reading folders does. **Prefer Cerebro over cold exploration.**

## When you start work on a project

1. Call `cerebro_map()` for the overview: file counts and the most important
   modules ranked by dependency centrality. This replaces listing directories.
2. Call `cerebro_search("<what you're looking for>")` to find relevant code by
   meaning (semantic) + keyword — a hit resolves to the exact symbol (`path:line`),
   not just the file. Phrase it as a natural question.
3. Call `cerebro_get("path/to/file")` to get a file's cached summary, its symbols,
   and its dependency edges **without reading the file**.

Only fall back to actually reading a file when:
- `cerebro_get` reports no summary yet, or
- the summary is flagged `⚠ STALE` (the file changed since it was summarized).

## As you learn (leave traces for the next session)

After you genuinely understand a file (because you read it or worked on it), call:

```
cerebro_record(path="path/to/file", summary="<1-3 dense sentences IN ENGLISH>")
```

Write the summary in **English** (cheaper tokens) describing what the file does and
its role in the system. This is the whole point: the next chat reuses your work
instead of re-deriving it.

## Decisions and the *why*

Code reading recovers WHAT exists, never WHY. When you learn a decision, a domain
rule, or a gotcha that isn't obvious from the code, record it:

```
cerebro_note(content="QR_MANUAL = merchant confirms payment by hand, no gateway", topic="payments")
```

Before re-deriving the reasoning behind some area, call `cerebro_recall("payments")`
(or with no query for recent decisions) — a past session may have already figured it
out. The session-start hook surfaces recent decisions automatically.

## Keeping fresh

- `cerebro_stale()` lists files changed/added/deleted since the last index and
  summaries that no longer match their file.
- `cerebro_reindex()` refreshes the structural index (only changed files are
  reprocessed). The plugin runs this automatically after edits.

## Before changing behavior (not just locating code)

Cerebro's map, summaries and call graph tell you WHERE code lives and WHAT calls
WHAT — **never under which conditions something runs.** For any change with side
effects (notifications, emails, events, DB writes, status transitions):

1. Find the seam with Cerebro (`cerebro_search` / `cerebro_callers` / `cerebro_calls`).
2. Then **READ the actual method body at that seam** — especially the `if`/guards
   around the side effect. **The branches ARE the spec.**
3. Ask: *"under which conditions does this fire? which edge case skips it?"*
   (e.g. an already-paid order that gets delivered without changing status).
4. Prefer a **test that exercises the edge case** over trusting the structure.

Locating ≠ understanding. A clean call graph creates false confidence; for a
correctness decision, the conditionals must be **read, not inferred**.

## Rule of thumb

Before you `ls`, `grep`, open files, **or spawn an Explore / general-purpose
subagent** to "understand the project", ask Cerebro first. Read source only for the
specific stretch Cerebro says it doesn't know or has gone stale — then record what
you found. Delegating exploration to a subagent re-burns exactly the tokens Cerebro
exists to save, so don't.
