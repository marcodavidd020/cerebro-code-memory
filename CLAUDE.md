# CLAUDE.md — Cerebro

Estándares reales de este proyecto. Cárgalos antes de proponer o escribir código.
Si algo aquí contradice al código actual, **gana el código**: verifícalo y actualiza este archivo.

## Qué es
MCP server en Python que cachea el *entendimiento* de un codebase en un SQLite ("brain")
para que sesiones de chat lo **consulten** en vez de re-leer carpetas. La consigna de todo
el proyecto: **salida token-barata**.

## Stack (verificado en `pyproject.toml`)
- Python **>=3.10**. Server con `FastMCP` (paquete `mcp>=1.2.0`).
- Deps núcleo: `tree-sitter` + `tree-sitter-language-pack`, `networkx`, `pathspec`.
- Semántica **opcional** detrás del extra `semantic`: `model2vec`, `numpy`. **Sin torch, sin API keys, nada sale de la máquina** — invariante de privacidad.
- Tests: `pytest` (en `.venv`). Build: `hatchling`, paquete en `src/cerebro`.

## Arquitectura (una responsabilidad por módulo, en `src/cerebro/`)
- `server.py` — superficie de tools MCP (`@mcp.tool()`). Delgada: arma texto compacto y delega la lógica.
- `cli.py` — entrypoint CLI unificado (`cerebro <subcomando>`).
- `config.py` — `Config.load()`; resuelve raíz (`CEREBRO_ROOT` o git) y `db_path`.
- `db.py` — conexión SQLite + queries de bajo nivel (módulo más central; casi todo depende de él).
- `indexer.py` — parseo tree-sitter: símbolos, edges, hashes; `reindex`/`diff`.
- `graph.py` — grafo de dependencias (`dependencies`/`dependents`) sobre networkx.
- `insights.py` — `impact`, `cycles`, `orphans`, `dead_symbols`.
- `callgraph.py` — `callers`/`calls` (resueltos por nombre).
- `summaries.py` / `summarizer.py` — guardar/leer resúmenes; warming batch vía `claude -p` headless.
- `embeddings.py` — búsqueda semántica model2vec (opcional).
- `notes.py` — log de decisiones. `gitsync.py` — frescura git-aware. `tsconfig.py` — alias tsconfig/jsconfig.
- `views.py` — render de texto (`map_text`, `recall_text`). `viz.py` — HTML del grafo + export Obsidian. `docaudit.py` — living docs.

## Convenciones (observadas en el código, respétalas)
- `from __future__ import annotations`; type hints modernos (`X | None`, no `Optional`).
- Toda tool MCP devuelve **string compacto**. Acota listas con helpers tipo `_join_capped` y caps (`_DEP_CAP`, `_SYM_CAP`). No vuelques datos sin límite: el ahorro de tokens es el producto.
- Docstrings explican el **porqué** (economía de tokens, invariantes), no lo obvio.
- Rutas: **siempre** normaliza a clave repo-relativa con `_resolve_path` antes de tocar la DB. Saltarse esto rompe la persistencia entre sesiones.
- Resúmenes y notas del brain se escriben **en inglés** (tokeniza más barato), densos, 1-3 frases.
- Scripts CLI: cada uno expone un `main()` a nivel módulo (ver `[project.scripts]`).
- Tests: fixtures de DB en memoria construyendo edges a mano (ver `tests/`).

## Cómo agregar una tool MCP nueva (patrón completo)
1. Lógica en el módulo de dominio que corresponda (no en `server.py`).
2. `@mcp.tool()` en `server.py` que llama a esa lógica y arma el texto compacto.
3. Subcomando equivalente en `cli.py`.
4. Test en `tests/` con fixture en memoria.

## Comandos
- Tests: `uv run pytest`  (o `.venv/bin/pytest`).
- Reindexar este propio repo: `uv run cerebro index`.
- Lint: **no hay ruff/mypy configurados**. Si quieres lint puntual: `uvx ruff check src/`.

## Qué NO hacer (guardarraíles anti-alucinación)
- No inventes nombres de tools: el set canónico vive en `server.py` (`cerebro_*`).
- No agregues dependencias a la ligera; lo pesado/opcional va detrás de un extra.
- No engordes la salida de las tools ni te saltes los caps.
- No escribas resúmenes/notas en español dentro del brain.
- No accedas a la DB con una ruta cruda: pasa por `_resolve_path`.
- No introduzcas torch, llamadas a APIs externas, ni nada que saque datos de la máquina.
