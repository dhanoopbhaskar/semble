"""Command-line interface for the Semble graph sidecar."""

from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
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
from semble_graph.search import graph_search, run_find_related, run_search
from semble_graph.version import __version__

_POSITION = re.compile(r"^(?P<path>.+?):(?P<line>\d+)$")
_CLI_COMMANDS = frozenset(
    {
        "build",
        "status",
        "doctor",
        "mcp",
        "symbol",
        "neighbors",
        "impact",
        "overview",
        "path",
        "search",
        "find-related",
        "file-types",
        "savings",
        "clear",
        "install",
        "uninstall",
        "-h",
        "--help",
        "-V",
        "--version",
    }
)


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


def _add_content_args(parser: argparse.ArgumentParser, default: tuple[str, ...] = ("code",)) -> None:
    """Add --content and the deprecated --include-text-files, mirroring Semble."""
    parser.add_argument(
        "--content",
        nargs="+",
        default=list(default),
        choices=[*compat.CONTENT_NAMES, "all"],
        metavar="TYPE",
        help="Content types to search (space-separated, e.g. --content code docs). "
        "Choices: code, docs, config, all. Default: code.",
    )
    parser.add_argument(
        "--include-text-files",
        action="store_true",
        help="Deprecated. Use --content all instead.",
    )


def _add_query_args(parser: argparse.ArgumentParser, *, graph: bool = False) -> None:
    """Add the Semble query arguments shared by search and find-related."""
    parser.add_argument(
        "path",
        nargs="*",
        default=None,
        metavar="PATH",
        help="Local paths or git URLs to search together (default: current directory).",
    )
    parser.add_argument("-k", "--top-k", type=int, default=5, help="Number of results (default: 5).")
    parser.add_argument(
        "--max-snippet-lines",
        type=int,
        default=None,
        metavar="N",
        help="Lines of source per result (default: full chunk). 10 = signature + body, 0 = no code.",
    )
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format (default: json).")
    _add_content_args(parser)
    if graph:
        parser.add_argument("--repo", help="Single repository path or git URL (alias for a positional PATH).")
        parser.add_argument("--token-budget", type=int, help="Optional output token budget for graph search.")
        parser.add_argument(
            "--expand",
            choices=("graph", "none"),
            default="none",
            help="Augment results with the code graph (default: none, matching Semble).",
        )
        parser.add_argument("--cache-dir", help="Override the sidecar cache directory.")


def _resolve_content(content: list[str], include_text_files: bool) -> tuple[str, ...]:
    """Resolve --content and the deprecated --include-text-files into content names."""
    if include_text_files:
        warnings.warn(
            "--include-text-files is deprecated and will be removed in a future version. Use --content all instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    return compat.content_names(content, include_text_files)


def _paths(args: argparse.Namespace) -> list[str]:
    """Resolve positional paths and the --repo alias into a source list."""
    paths = list(getattr(args, "path", None) or [])
    if not paths and getattr(args, "repo", None):
        paths = [args.repo]
    return paths or ["."]


def _emit(value: Any, fmt: str, kind: str | None = None) -> None:
    if fmt == "text":
        print(render_text(value, kind))
    else:
        print(render_json(value))


def _print_results(out: dict[str, Any], output_format: str) -> None:
    """Print a Semble result payload exactly like Semble's CLI."""
    if output_format == "json":
        print(json.dumps(out))
    elif "error" in out:
        print(out["error"])
    else:
        for result in out["results"]:
            snippet = f"\n\n{result['content']}" if "content" in result else ""
            print(f"{result['file_path']}:{result['start_line']}-{result['end_line']}{snippet}\n")


def _load(repo: str, settings: Settings, *, rebuild: bool = True) -> LoadedGraph:
    loaded = get_service(repo, settings).load(rebuild=rebuild)
    if loaded is None:
        raise SystemExit(f"Could not load or build a graph for {repo!r}.")
    return loaded


def _require_semble() -> int | None:
    """Return an exit code when Semble is unavailable, otherwise ``None``."""
    if compat.semble_available():
        return None
    print("Semble is not installed. Install it with: pip install 'semble-graph[search]'", file=sys.stderr)
    return 1


def _run_installer(args: argparse.Namespace) -> int:
    if args.type and not args.agent:
        raise SystemExit("--type requires --agent")
    unavailable = _require_semble()
    if unavailable is not None:
        return unavailable
    integration_ids = None if not args.type or "all" in args.type else list(args.type)
    compat.run_installer(args.command, args.agent, integration_ids, args.yes)
    return 0


def _run_search(args: argparse.Namespace, settings: Settings) -> int:
    paths = _paths(args)
    content = _resolve_content(args.content, args.include_text_files)
    if args.expand == "graph":
        value = graph_search(
            paths[0],
            args.query,
            top_k=args.top_k,
            token_budget=args.token_budget,
            expand=True,
            settings=settings,
            content=content,
        )
        _emit(value, args.format, "search")
        return 0
    try:
        value = run_search(
            paths,
            args.query,
            top_k=args.top_k,
            max_snippet_lines=args.max_snippet_lines,
            content=content,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_results(value, args.format)
    return 0


def _run_find_related(args: argparse.Namespace) -> int:
    paths = _paths(args)
    content = _resolve_content(args.content, args.include_text_files)
    try:
        value = run_find_related(
            paths,
            args.file_path,
            args.line,
            top_k=args.top_k,
            max_snippet_lines=args.max_snippet_lines,
            content=content,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if value is None:
        print(f"No chunk found at {args.file_path}:{args.line}.", file=sys.stderr)
        return 1
    _print_results(value, args.format)
    return 0


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

        serve(content=_resolve_content(args.content, args.include_text_files))
        return 0

    if args.command == "clear":
        unavailable = _require_semble()
        if unavailable is not None:
            return unavailable
        compat.clear_cache(args.type)
        return 0

    if args.command == "file-types":
        unavailable = _require_semble()
        if unavailable is not None:
            return unavailable
        print(json.dumps(compat.file_types(compat.content_names(args.content, False))))
        return 0

    if args.command == "savings":
        unavailable = _require_semble()
        if unavailable is not None:
            return unavailable
        print(compat.savings_report())
        return 0

    if args.command in ("install", "uninstall"):
        return _run_installer(args)

    if args.command == "search":
        return _run_search(args, settings)

    if args.command == "find-related":
        return _run_find_related(args)

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

    raise SystemExit(f"Unknown command: {args.command}")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="semble-graph",
        description="Instant code search plus a deterministic code graph for Semble.",
    )
    parser.add_argument("-V", "--version", action="version", version=__version__)
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

    search = subparsers.add_parser("search", help="Search one or more repos (Semble-compatible).")
    search.add_argument("query")
    _add_query_args(search, graph=True)

    related = subparsers.add_parser("find-related", help="Find code similar to a specific location.")
    related.add_argument("file_path", help="File path as shown in search results.")
    related.add_argument("line", type=int, help="Line number (1-indexed).")
    _add_query_args(related)

    file_types = subparsers.add_parser("file-types", help="List the file extensions indexed per content type.")
    _add_content_args(file_types, default=("all",))

    subparsers.add_parser("savings", help="Show token savings and usage stats.")

    clear = subparsers.add_parser("clear", help="Clear the Semble index cache.")
    clear.add_argument(
        "type",
        choices=["all", "index", "savings", "orphans"],
        help="Type of cache to clear. `orphans` removes indexes whose source path no longer exists.",
    )

    for command, verb in (("install", "configure"), ("uninstall", "remove configuration from")):
        installer = subparsers.add_parser(command, help=f"{verb.capitalize()} Semble across coding agents.")
        installer.add_argument(
            "--agent",
            nargs="+",
            choices=compat.agent_ids() or None,
            metavar="AGENT",
            help=f"Agent(s) to {verb} non-interactively, e.g. --agent claude pi. Skips prompts.",
        )
        installer.add_argument(
            "--type",
            nargs="+",
            choices=[*compat.integration_types(), "all"] or None,
            metavar="TYPE",
            help="Integrations to include (mcp, instructions, subagent, or all). Default: all. Requires --agent.",
        )
        installer.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt. Combine with --agent for a fully non-interactive run.",
        )

    mcp = subparsers.add_parser("mcp", help="Run the MCP server over stdio.")
    _add_content_args(mcp)

    return parser


def _mcp_main(argv: list[str]) -> int:
    """Run the MCP server (Semble does this for a bare invocation)."""
    parser = argparse.ArgumentParser(
        prog="semble-graph",
        description="Instant code search plus a deterministic code graph for local repositories.",
    )
    _add_content_args(parser)
    args = parser.parse_args(argv)
    from semble_graph.mcp import serve

    serve(content=_resolve_content(args.content, args.include_text_files))
    return 0


def _run_cli(argv: list[str]) -> int:
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


def main(argv: list[str] | None = None) -> int:
    """Run the CLI, or the MCP server when no known command is given."""
    args = list(sys.argv[1:]) if argv is None else list(argv)
    if not args or args[0] not in _CLI_COMMANDS:
        return _mcp_main(args)
    return _run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
