# Semble Graph

A deterministic code-graph sidecar for [Semble](https://github.com/MinishLab/semble).

`semble-graph` adds exact symbol lookup, callers/callees, importers, inheritance,
impact analysis, repository overview, and graph-aware search **without changing
Semble**. It is a separate package, process, cache, and MCP server. Semble does
not import, detect, or depend on it.

The sidecar is a **superset** of Semble's MCP and CLI surface: `search` and
`find_related` accept the same arguments and return the same payloads, and every
Semble CLI command is available (`search`, `find-related`, `file-types`,
`savings`, `clear`, `install`, `uninstall`). Graph tools and commands are added on
top.

## Why a sidecar?

- **Fail open.** A missing, stale, or corrupt graph never blocks Semble search.
- **Public API only.** Semble is imported in exactly one file,
  `src/semble_graph/compat.py`, through `semble.__all__`.
- **Independent storage.** The sidecar owns `~/.cache/semble-graph`. Graph
  extraction never writes into Semble's cache; Semble-backed commands reuse
  Semble's own index cache.
- **Superset surface.** MCP exposes Semble-compatible `search` and `find_related`
  plus `graph_*` tools; the CLI mirrors Semble's commands and adds graph ones.
- **Deterministic extraction.** Tree-sitter only. No network, LLM, or model
  calls during graph extraction.

## Install

From this repository:

```bash
cd packages/semble-graph
pip install -e .
```

Or directly from the repository root without changing directory:

```bash
pip install "./packages/semble-graph[mcp,search]"
```

Once the sidecar package is committed and pushed, it can also be installed
directly from the git repository:

```bash
pipx install "semble-graph[mcp,search] @ git+https://github.com/dhanoopbhaskar/semble.git#subdirectory=packages/semble-graph"
```

`pip install .` from the repository root installs Semble itself, not the
sidecar. Point at `packages/semble-graph` explicitly.

> If you use `uv pip install` from the repository root, add `--no-config` or
> run it from inside `packages/semble-graph`. The root `[tool.uv]
> exclude-newer` setting currently filters out Semble `0.7.0`, which makes the
> `search` extra unsatisfiable from the root.

Optional extras:

```bash
pip install -e ".[search]"   # Semble-backed search, find_related, graph_search
pip install -e ".[mcp]"      # MCP server
pip install -e ".[dev]"      # tests, ruff, mypy
```

Graph tools work without Semble. `semble` is required for `search`,
`find_related`, `graph_search`, and the Semble-compatible CLI commands
(`file-types`, `savings`, `clear`, `install`, `uninstall`).

## Quick start

```bash
# Semble-compatible commands
semble-graph search "retry policy" ./repo
semble-graph find-related src/payments/service.py 42 ./repo
semble-graph file-types
semble-graph savings
semble-graph clear index

# Graph commands
semble-graph build ./repo
semble-graph status ./repo
semble-graph doctor

semble-graph symbol PaymentService --repo ./repo
semble-graph neighbors src/payments/service.py:42 --repo ./repo --relation callers
semble-graph impact src/payments/service.py --repo ./repo --depth 3
semble-graph overview ./repo

# Graph-augmented search (opt-in; the default matches Semble exactly)
semble-graph search "retry policy" ./repo --expand graph
```

`search` and `find-related` accept multiple paths or git URLs, `-k/--top-k`,
`--max-snippet-lines`, `--content`, and `--format`, just like Semble. Output is
JSON by default for agents; add `--format text` for humans.

Bare `semble-graph` (or `semble-graph --content ...`) starts the MCP server,
matching `semble`'s dispatch.

## MCP configuration

Run `semble-graph` on its own (it already exposes Semble's tools):

```json
{
  "mcp": {
    "semble-graph": {
      "command": "semble-graph",
      "args": ["mcp"]
    }
  }
}
```

Or run it beside Semble if you want both processes:

```json
{
  "mcp": {
    "semble": {
      "command": "semble",
      "args": ["mcp"]
    },
    "semble-graph": {
      "command": "semble-graph",
      "args": ["mcp"]
    }
  }
}
```

Tools:

| Tool | Purpose |
|---|---|
| `search` | Semble-compatible search (git URLs, multiple repos, `content`, snippets) |
| `find_related` | Semble-compatible related-code lookup |
| `graph_symbol` | Exact/qualified definitions plus direct callers, callees, importers |
| `graph_neighbors` | Neighbouring symbols by relation, depth, confidence, evidence line |
| `graph_impact` | Reverse-reachable dependants for changed paths or symbols |
| `graph_overview` | Counts, core symbols by degree, likely entry points |
| `graph_path` | Shortest relation path between two symbols |
| `graph_search` | Semble retrieval augmented with exact definitions and graph neighbours |

`search` and `find_related` are backed by the Semble **library** (not by
proxying Semble's MCP server) and return Semble's exact result payloads.

## Supported languages

v1 tree-sitter extraction:

- Python (`.py`, `.pyi`)
- TypeScript / TSX (`.ts`, `.mts`, `.cts`, `.tsx`)
- JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`)
- Go (`.go`)
- Rust (`.rs`)
- Java (`.java`)

Unknown languages contribute no symbols. A missing grammar never fails an
index build.

## Graph model

Symbols: file, module, function, method, class, type, constant.

Edges: `defines`, `contains`, `imports`, `imports_from`, `calls`, `inherits`,
`implements`, `tests`, and `references`.

Every edge carries one of:

- `EXTRACTED` — direct lexical, import, or same-file evidence.
- `INFERRED` — unique repo-wide name match.
- `AMBIGUOUS` — multiple candidates; never presented as definite.

Each edge also carries the source line that produced it when available.

Resolution tiers:

1. Same file / lexical scope.
2. Imported alias → module → file.
3. Same package/module directory.
4. Repo-wide unique name.
5. Multiple candidates → `AMBIGUOUS`.

## How search integration works

`graph_search` is a **sidecar tool**. Semble's ranking is not modified.

1. Candidate retrieval through Semble's public `search`.
2. Map chunks to graph symbols by file and line overlap.
3. Route intent: symbol, relationship, or conceptual.
4. Prepend exact definitions for symbol queries; traverse callers/importers/etc.
   for relationship queries.
5. Add a capped one-hop graph-proximity bonus to semantic candidates.
6. Deduplicate by symbol and pack snippets to an optional token budget.

Graph bonuses are deliberately small. Inferred edges never outrank an exact
definition. If graph augmentation fails, plain Semble results are returned with
`"graph_status": "unavailable"`.

## Cache and freshness

```
~/.cache/semble-graph/<key>/
├── graph/
│   ├── graph.meta.json
│   ├── symbols.json
│   ├── edges.npz
│   └── names.json
├── raw_facts.json
└── build.lock
```

- `SEMBLE_GRAPH_CACHE_DIR` overrides the cache root.
- `SEMBLE_MAX_FILE_BYTES` caps indexed file size (default `1_000_000`).
- Incremental builds reuse raw facts by `(mtime_ns, size)`.
- Global resolution runs on every build.
- Builds write to a temporary directory and atomically swap the current graph.
- Small repositories rebuild synchronously when stale. Large repositories
  (`SEMBLE_GRAPH_STALE_BACKGROUND_FILES`, default `5000`) serve the stale graph
  with `"graph_status": "stale"` and rebuild in the background.
- The sidecar refuses a cache root inside `SEMBLE_CACHE_LOCATION`.

## Compatibility matrix

| Sidecar | Semble | Status |
|---|---|---|
| `0.1.x` | `>=0.7.0,<0.8.0` | Supported |

The sidecar checks `importlib.metadata.version("semble")` lazily on the first
Semble-backed call. Outside the supported range it warns once, then runs
optimistically. `semble-graph doctor` prints both versions and the verdict.

Contract tests cover `semble.__all__`, `SembleIndex.from_path`, `Chunk` fields,
`search`, `find_related`, and `index.chunks`. Parity tests assert that Semble's
MCP tool schemas and CLI commands/options are a subset of the sidecar's. A test
enforces that no module other than `compat.py` imports Semble.

## Failure behavior

| Failure | Behavior |
|---|---|
| Graph missing | Build on demand |
| Graph corrupt | Rebuild |
| Graph stale | Sync rebuild, or stale + background rebuild on large repos |
| Semble missing | Graph tools still work; `search`, `find_related`, and Semble-backed CLI commands return a clear error |
| Semble API mismatch | Warn once; adapter failures fail open at the tool boundary |
| MCP graph tool error | Structured JSON error, never an uncaught exception |
| MCP `search` / `find_related` error | Semble-compatible text, matching Semble's MCP server |

## Development

```bash
cd packages/semble-graph
PYTHONPATH=src python -m pytest
PYTHONPATH=src python -m pytest -m contract
python -m ruff check src tests
python -m ruff format --check src tests
```

Benchmarks:

```bash
PYTHONPATH=src python benchmarks/bench.py --repo ../..
PYTHONPATH=src python benchmarks/bench.py --repo ../.. --search
```

## Scope

Graph extraction targets local repository paths. `search`, `find-related`, and
`graph_search` accept remote https/http git URLs and multiple repos through
Semble. Communities beyond degree-based overview and a Graphify-backed extractor
are later work.
