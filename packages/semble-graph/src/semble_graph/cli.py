"""Command-line interface for the Semble graph sidecar."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from semble_graph import compat
from semble_graph.cache import default_cache_dir
from semble_graph.config import EXTRACTOR_VERSION, SCHEMA_VERSION, Settings, get_settings
from semble_graph.graph.build import build_graph, graph_status
from semble_graph.graph.model import SymbolKind
from semble_graph.graph.query import (
    LoadedGraph,
    get_service,
    graph_impact,
    graph_neighbors,
    graph_overview,
    graph_path,
    graph_symbol,
)
from semble_graph.render import render_json, render_text
from semble_graph.search import graph_search, plain_search
from semble_graph.version import __version__

_POSITION = re.compile(r"^(?P<path>.+?):(?P<line>\d+)$")


def _settings(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    if getattr(args, "cache_dir", None):
        from dataclasses import replace

        settings = replace(settings, cache_dir=Path(args.cache_dir).expanduser())
    return settings


def _add_common(parser: argparse.ArgumentParser, *, repo: bool = True) -> None:
    if repo:
        parser.add_argument("--repo", default=".", help="Repository path (default: current directory).")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Output format.")
    parser.add_argument("--cache-dir", help="Override the sidecar cache directory.")


def _emit(value: Any, fmt: str, kind: str | None = None) -> None:
    if fmt == "text":
        print(render_text(value, kind))
    else:
        print(render_json(value))


def _load(repo: str, settings: Settings, *, rebuild: bool = True) -> LoadedGraph:
    loaded = get_service(repo, settings).load(rebuild=rebuild)
    if loaded is None:
        raise SystemExit(f"Could not load or build a graph for {repo!r}.")
    return loaded


def _run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.command == "build":
        result = build_graph(
            args.repo,
            settings,
            force=args.force,
            use_semble_files=args.semble_files,
        )
        payload: dict[str, Any] = {
            "repo": str(Path(args.repo).expanduser().resolve()),
            "status": "built",
            "cache_path": str(result.layout.graph_dir),
            "stats": {
                "files": result.stats.files,
                "parsed": result.stats.parsed,
                "reused": result.stats.reused,
                "skipped_large": result.stats.skipped_large,
                "symbols": result.stats.symbols,
                "edges": result.stats.edges,
                "seconds": round(result.stats.seconds, 3),
                "truncated": result.stats.truncated,
                "languages": result.stats.languages,
            },
        }
        _emit(payload, args.format)
        return 0

    if args.command == "status":
        _emit(graph_status(args.repo, settings), args.format, "status")
        return 0

    if args.command == "doctor":
        try:
            from semble_grammars import available_languages
        except Exception:
            available_languages = lambda: []  # noqa: E731
        status = compat.status()
        doctor_payload: dict[str, Any] = {
            "sidecar_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "cache_dir": str(settings.cache_dir or default_cache_dir()),
            "supported_languages": ["python", "typescript", "tsx", "javascript", "go", "rust", "java"],
            "grammars_available": available_languages(),
            "max_file_bytes": settings.max_file_bytes,
            "semble": status,
            "verdict": "semble-backed features available" if status["supported"] else "graph-only / optimistic",
        }
        _emit(doctor_payload, args.format, "status")
        return 0

    if args.command == "mcp":
        from semble_graph.mcp import serve

        serve(include_search=args.include_search)
        return 0

    service = _load(args.repo, settings)
    loaded = service
    graph = loaded.graph

    if args.command == "symbol":
        value = graph_symbol(graph, args.name, kind=args.kind, include_relations=not args.no_relations)
        value["graph_status"] = loaded.status
        _emit(value, args.format, "symbol")
        return 0

    if args.command == "neighbors":
        target = args.target
        symbol: str | None = None
        file_path: str | None = None
        line: int | None = None
        match = _POSITION.match(target)
        if match:
            file_path = match.group("path")
            line = int(match.group("line"))
        elif "/" in target or "\\" in target:
            file_path = target
        else:
            symbol = target
        value = graph_neighbors(
            graph,
            symbol=symbol,
            file_path=file_path,
            line=line,
            relation=args.relation,
            depth=args.depth,
        )
        value["graph_status"] = loaded.status
        _emit(value, args.format, "neighbors")
        return 0

    if args.command == "impact":
        relations = [item.strip() for item in args.relations.split(",") if item.strip()] if args.relations else None
        value = graph_impact(
            graph,
            paths=args.paths,
            symbols=args.symbols,
            depth=args.depth,
            relations=relations,
        )
        value["graph_status"] = loaded.status
        _emit(value, args.format, "impact")
        return 0

    if args.command == "overview":
        value = graph_overview(graph, top_n=args.top_n)
        value["graph_status"] = loaded.status
        _emit(value, args.format, "overview")
        return 0

    if args.command == "path":
        value = graph_path(graph, args.source, args.target, max_hops=args.max_hops)
        value["graph_status"] = loaded.status
        _emit(value, args.format)
        return 0

    if args.command == "search":
        if args.expand == "graph":
            value = graph_search(
                args.repo,
                args.query,
                top_k=args.top_k,
                token_budget=args.token_budget,
                expand=True,
                settings=settings,
            )
        else:
            value = plain_search(args.repo, args.query, top_k=args.top_k)
        _emit(value, args.format, "search")
        return 0

    raise SystemExit(f"Unknown command: {args.command}")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="semble-graph",
        description="Deterministic code-graph sidecar for Semble.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build or incrementally update a repository graph.")
    build.add_argument("repo", nargs="?", default=".")
    build.add_argument("--force", action="store_true", help="Ignore the raw-facts cache and reparse every file.")
    build.add_argument("--semble-files", action="store_true", help="Use Semble's indexed file set when available.")
    _add_common(build, repo=False)

    status = subparsers.add_parser("status", help="Report graph freshness and cache information.")
    status.add_argument("repo", nargs="?", default=".")
    _add_common(status, repo=False)

    doctor = subparsers.add_parser("doctor", help="Print version and compatibility diagnostics.")
    _add_common(doctor, repo=False)

    symbol = subparsers.add_parser("symbol", help="Look up an exact or qualified symbol.")
    symbol.add_argument("name")
    symbol.add_argument("--kind", choices=[kind.value for kind in SymbolKind])
    symbol.add_argument("--no-relations", action="store_true", help="Omit direct relations.")
    _add_common(symbol)

    neighbors = subparsers.add_parser("neighbors", help="Find neighbouring symbols.")
    neighbors.add_argument("target", help="Symbol name or file_path:line.")
    neighbors.add_argument("--relation", help="Relation or role (calls, callers, imports, importers, tests, ...).")
    neighbors.add_argument("--depth", type=int, default=1)
    _add_common(neighbors)

    impact = subparsers.add_parser("impact", help="Compute reverse-reachable impact.")
    impact.add_argument("paths", nargs="*", help="Changed file or directory paths.")
    impact.add_argument("--symbol", dest="symbols", action="append", default=[], help="Changed symbol name.")
    impact.add_argument("--depth", type=int, default=2)
    impact.add_argument("--relations", help="Comma-separated relation kinds.")
    _add_common(impact)

    overview = subparsers.add_parser("overview", help="Show core symbols, entry points, and counts.")
    overview.add_argument("repo", nargs="?", default=".")
    overview.add_argument("--top-n", type=int, default=15)
    _add_common(overview, repo=False)

    path = subparsers.add_parser("path", help="Find a shortest relation path between symbols.")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-hops", type=int, default=6)
    _add_common(path)

    search = subparsers.add_parser("search", help="Graph-aware search (requires Semble).")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--token-budget", type=int)
    search.add_argument("--expand", choices=("graph", "none"), default="graph")
    _add_common(search)

    mcp = subparsers.add_parser("mcp", help="Run the graph MCP server over stdio.")
    mcp.add_argument("--include-search", action="store_true", help="Also expose Semble-backed search/find_related.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except SystemExit:
        raise
    except Exception as exc:
        payload = {"error": str(exc), "type": type(exc).__name__}
        if getattr(args, "format", "json") == "text":
            print(f"error: {exc}", file=sys.stderr)
        else:
            print(render_json(payload))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
