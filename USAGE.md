# Cerebro — daily driver

How to actually use it day to day. Set up once, then it mostly runs itself.

## Once per repo

```bash
uv tool install --from ~/Proyectos/cerebro cerebro     # one time, global `cerebro`
cd /your/repo
cerebro setup --summarize --embed                       # index + warm summaries + semantic index
# then run the `claude mcp add …` line it prints, and reload your editor
```

Hooks live in `~/.claude/settings.json` (SessionStart injects the map; PostToolUse keeps
the index fresh). The skill in `~/.claude/skills/cerebro/` tells the model to use it.

## Day to day (mostly automatic)

- **New chat** → the map + recorded decisions are injected automatically. Just work.
- **You edit a file** → it reindexes that file (~0.3s). Nothing to do.
- **The model** queries `cerebro_*` instead of grepping; it records summaries/decisions as it learns.

You only reach for commands when you want something specific:

| Want | Command (or just ask the model) |
|---|---|
| Find code by intent | `cerebro search "where do we validate stock at checkout?"` |
| Understand a file w/o reading it | `cerebro_get <path>` (in chat) |
| Blast radius before a change | `cerebro impact <path>` |
| Who calls this | `cerebro callers <name>` |
| Architecture smells | `cerebro cycles` · `cerebro orphans` (dead files) · `cerebro dead-symbols` (unused exports) |
| See the whole project | `cerebro graph` → one HTML graph; `cerebro graph-all` regenerates the scoped graphs declared in `.cerebro/graphs.toml` |
| Browse as a wiki | `cerebro obsidian` → open the vault in Obsidian |

## Keep it warm (on demand, not scheduled)

```bash
cerebro index --force          # re-extract everything (after an extractor upgrade)
cerebro summarize --limit 50   # more cached summaries (uses claude -p, no API key)
cerebro embed                  # refresh the semantic index after summaries change
```

Optional: set `CEREBRO_AUTOSUMMARIZE=N` in your environment to let the SessionStart
hook warm the `N` most-central un-summarized files in the background each session
(detached, non-blocking). **Off by default** — it spends tokens via `claude -p`, so
opt in deliberately. Coverage then grows on its own as you work.

## Living docs (if you keep a markdown knowledge vault)

```bash
cerebro doc-audit  <vault> --aliases "backend_app=fenix-store-backend,…"   # flag stale notes
cerebro doc-refresh <note>                                                  # re-audit one note vs live code
```

## Rule of thumb

Don't babysit it. Use it for real work; if something feels slow or wrong, *measure it*
(`cerebro <cmd>` timings) and fix that specific thing — not everything.
