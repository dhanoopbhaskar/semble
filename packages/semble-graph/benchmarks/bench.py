"""Small benchmark harness for the graph sidecar.

Usage::

    PYTHONPATH=src python benchmarks/bench.py --repo ../..
    PYTHONPATH=src python benchmarks/bench.py --repo ../.. --search
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from semble_graph.cache import get_cache_layout
from semble_graph.config import get_settings
from semble_graph.graph.build import build_graph
from semble_graph.graph.query import graph_neighbors, graph_overview, graph_symbol
from semble_graph.graph.store import load_graph


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((percentile / 100) * (len(ordered) - 1)))))
    return ordered[index]


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--search", action="store_true", help="Also benchmark graph_search vs plain search.")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    settings = get_settings()
    layout = get_cache_layout(repo, settings)

    started = time.perf_counter()
    full = build_graph(repo, settings, force=True)
    full_seconds = time.perf_counter() - started
    started = time.perf_counter()
    incremental = build_graph(repo, settings)
    incremental_seconds = time.perf_counter() - started

    started = time.perf_counter()
    graph = load_graph(layout)
    load_seconds = time.perf_counter() - started
    if graph is None:
        raise SystemExit("Failed to load the graph after building it.")

    overview = graph_overview(graph, top_n=8)
    symbols = [item["qualified_name"] for item in overview["core_symbols"]] or ["main"]
    symbol_latencies: list[float] = []
    neighbor_latencies: list[float] = []
    for index in range(args.iterations):
        name = symbols[index % len(symbols)]
        started = time.perf_counter()
        graph_symbol(graph, name)
        symbol_latencies.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        graph_neighbors(graph, symbol=name, depth=1)
        neighbor_latencies.append((time.perf_counter() - started) * 1000)

    payload = {
        "repo": str(repo),
        "files": full.stats.files,
        "symbols": full.stats.symbols,
        "edges": full.stats.edges,
        "cache_bytes": _directory_size(layout.key_dir),
        "full_build_seconds": round(full_seconds, 4),
        "incremental_build_seconds": round(incremental_seconds, 4),
        "incremental_parsed": incremental.stats.parsed,
        "incremental_reused": incremental.stats.reused,
        "incremental_ratio": round(incremental_seconds / full_seconds, 4) if full_seconds else None,
        "load_ms": round(load_seconds * 1000, 2),
        "symbol_query_p50_ms": round(statistics.median(symbol_latencies), 2),
        "symbol_query_p95_ms": round(_percentile(symbol_latencies, 95), 2),
        "neighbors_query_p50_ms": round(statistics.median(neighbor_latencies), 2),
        "neighbors_query_p95_ms": round(_percentile(neighbor_latencies, 95), 2),
    }

    if args.search:
        try:
            from semble_graph.search import graph_search, plain_search

            queries = [
                symbols[0] if symbols else "main",
                "where is the search pipeline implemented",
                f"who calls {symbols[0]}" if symbols else "who calls main",
            ]
            comparisons = []
            for query in queries:
                started = time.perf_counter()
                plain = plain_search(repo, query, top_k=5)
                plain_ms = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                augmented = graph_search(repo, query, top_k=5, settings=settings)
                augmented_ms = (time.perf_counter() - started) * 1000
                comparisons.append(
                    {
                        "query": query,
                        "plain_ms": round(plain_ms, 2),
                        "graph_ms": round(augmented_ms, 2),
                        "plain_tokens": sum(len(item.get("snippet", "")) // 4 for item in plain.get("results", [])),
                        "graph_tokens": sum(len(item.get("snippet", "")) // 4 for item in augmented.get("results", [])),
                        "graph_status": augmented.get("graph_status"),
                    }
                )
            payload["search"] = comparisons
        except Exception as exc:  # pragma: no cover - benchmark convenience
            payload["search_error"] = str(exc)

    import json

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
