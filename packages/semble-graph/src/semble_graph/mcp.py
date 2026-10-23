"""MCP server exposing Semble-compatible search plus graph_* tools."""

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from semble_graph import compat
from semble_graph.config import get_settings
from semble_graph.graph.query import (
    get_service,
)
from semble_graph.graph.query import (
    graph_impact as run_graph_impact,
)
from semble_graph.graph.query import (
    graph_neighbors as run_graph_neighbors,
)
from semble_graph.graph.query import (
    graph_overview as run_graph_overview,
)
from semble_graph.graph.query import (
    graph_path as run_graph_path,
)
from semble_graph.graph.query import (
    graph_symbol as run_graph_symbol,
)
from semble_graph.search import graph_search as run_graph_search
from semble_graph.search import run_find_related, run_search
from semble_graph.version import __version__

logger = logging.getLogger(__name__)

_REPO_DESCRIPTION = (
    "A local directory path or https:// or http:// git URL (e.g. https://github.com/org/repo) to index and "
    "search, or a list of them to search several repos at once. Indexes are cached after the first call, "
    "so repeat queries are fast. Multi-repo results prefix file_path with the repo name."
)
_GRAPH_REPO_DESCRIPTION = (
    "Local repository path to graph (e.g. /path/to/repo). The sidecar builds or loads its own cache; "
    "Semble's cache is never modified."
)
_GRAPH_CONTENT = Literal["code", "docs", "config", "all"]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _error(exc: Exception) -> str:
    logger.exception("Graph tool failed")
    return _json({"error": str(exc), "type": type(exc).__name__, "graph_status": "unavailable"})


def _load(repo: str) -> tuple[Any, str]:
    loaded = get_service(repo, get_settings()).load()
    if loaded is None:
        raise RuntimeError(f"Could not load or build a graph for {repo!r}.")
    return loaded.graph, loaded.status


def _resolve_content_selection(content: _GRAPH_CONTENT | None, default_content: Sequence[str]) -> tuple[str, ...]:
    """Resolve an MCP content selection to content names."""
    if content is None:
        return tuple(default_content)
    if content == "all":
        return compat.CONTENT_NAMES
    return (content,)


def _repo_error(repo: str | list[str], exc: Exception) -> str:
    """Format an index failure the way Semble's MCP server does."""
    source = repo if isinstance(repo, str) else ", ".join(repo)
    return f"Failed to index {source!r}: {exc}"


def create_server(default_content: Sequence[str] = ("code",)) -> Any:
    """Create the sidecar MCP server."""
    from mcp.server.mcpserver import MCPServer
    from pydantic import Field

    server = MCPServer(
        "semble-graph",
        version=__version__,
        instructions=(
            "Instant code search plus a deterministic code graph for local repositories and git URLs. "
            "Use `search` for text retrieval and `find_related` for similar code, then `graph_symbol` for "
            "exact definitions, `graph_neighbors` for callers/callees/importers, `graph_impact` for blast "
            "radius, and `graph_overview` for repository orientation. Every edge includes a confidence "
            "label and evidence line."
        ),
    )

    @server.tool()
    def graph_symbol(
        name: Annotated[str, Field(description="Symbol name or qualified name, e.g. PaymentService or Class.method.")],
        repo: Annotated[str, Field(description=_GRAPH_REPO_DESCRIPTION)],
        kind: Annotated[
            str | None,
            Field(description="Optional symbol kind: file, module, function, method, class, type, constant."),
        ] = None,
        include_relations: Annotated[bool, Field(description="Include direct callers, callees, and importers.")] = True,
    ) -> str:
        """Look up exact or qualified symbol definitions and their direct relations."""
        try:
            graph, status = _load(repo)
            value = run_graph_symbol(graph, name, kind=kind, include_relations=include_relations)
            value["graph_status"] = status
            return _json(value)
        except Exception as exc:
            return _error(exc)

    @server.tool()
    def graph_neighbors(
        repo: Annotated[str, Field(description=_GRAPH_REPO_DESCRIPTION)],
        symbol: Annotated[str | None, Field(description="Symbol name or qualified name.")] = None,
        file_path: Annotated[str | None, Field(description="Repository-relative file path.")] = None,
        line: Annotated[int | None, Field(description="1-indexed line number, used with file_path.")] = None,
        relation: Annotated[
            str | None,
            Field(description="Relation filter: calls, callers, callees, imports, importers, inherits, tests, ..."),
        ] = None,
        depth: Annotated[int, Field(description="Traversal depth (1-8).", ge=1, le=8)] = 1,
    ) -> str:
        """Find neighbouring symbols by relation, with confidence and evidence lines."""
        try:
            graph, status = _load(repo)
            value = run_graph_neighbors(
                graph,
                symbol=symbol,
                file_path=file_path,
                line=line,
                relation=relation,
                depth=depth,
            )
            value["graph_status"] = status
            return _json(value)
        except Exception as exc:
            return _error(exc)

    @server.tool()
    def graph_impact(
        repo: Annotated[str, Field(description=_GRAPH_REPO_DESCRIPTION)],
        paths: Annotated[list[str] | None, Field(description="Changed file or directory paths.")] = None,
        symbols: Annotated[list[str] | None, Field(description="Changed symbol names.")] = None,
        depth: Annotated[int, Field(description="Reverse traversal depth (1-8).", ge=1, le=8)] = 2,
        relations: Annotated[
            list[str] | None,
            Field(description="Optional relation kinds; defaults to calls/imports/inheritance/tests."),
        ] = None,
    ) -> str:
        """Compute reverse-reachable dependants (blast radius) for changed files or symbols."""
        try:
            graph, status = _load(repo)
            value = run_graph_impact(graph, paths=paths, symbols=symbols, depth=depth, relations=relations)
            value["graph_status"] = status
            return _json(value)
        except Exception as exc:
            return _error(exc)

    @server.tool()
    def graph_overview(
        repo: Annotated[str, Field(description=_GRAPH_REPO_DESCRIPTION)],
        top_n: Annotated[int, Field(description="Number of core symbols to return.", ge=1, le=100)] = 15,
    ) -> str:
        """Show repository counts, core symbols by degree, and likely entry points."""
        try:
            graph, status = _load(repo)
            value = run_graph_overview(graph, top_n=top_n)
            value["graph_status"] = status
            return _json(value)
        except Exception as exc:
            return _error(exc)

    @server.tool()
    def graph_path(
        source: Annotated[str, Field(description="Source symbol name.")],
        target: Annotated[str, Field(description="Target symbol name.")],
        repo: Annotated[str, Field(description=_GRAPH_REPO_DESCRIPTION)],
        max_hops: Annotated[int, Field(description="Maximum path length.", ge=1, le=12)] = 6,
    ) -> str:
        """Find a shortest relation path between two symbols."""
        try:
            graph, status = _load(repo)
            value = run_graph_path(graph, source, target, max_hops=max_hops)
            value["graph_status"] = status
            return _json(value)
        except Exception as exc:
            return _error(exc)

    @server.tool()
    def graph_search(
        query: Annotated[str, Field(description="Natural language, symbol, or relationship query.")],
        repo: Annotated[str, Field(description=_GRAPH_REPO_DESCRIPTION)],
        top_k: Annotated[int, Field(description="Number of results to return.", ge=1)] = 10,
        token_budget: Annotated[int | None, Field(description="Optional output token budget.")] = None,
        expand: Annotated[bool, Field(description="Expand top seeds by one graph hop.")] = True,
    ) -> str:
        """Graph-aware search: Semble retrieval augmented with exact definitions and graph neighbours."""
        try:
            return _json(
                run_graph_search(
                    repo,
                    query,
                    top_k=top_k,
                    token_budget=token_budget,
                    expand=expand,
                    settings=get_settings(),
                )
            )
        except Exception as exc:
            return _error(exc)

    @server.tool()
    async def search(
        query: Annotated[str, Field(description="Natural language or code query.")],
        repo: Annotated[str | list[str], Field(description=_REPO_DESCRIPTION)],
        top_k: Annotated[int, Field(description="Number of results to return.", ge=1)] = 5,
        max_snippet_lines: Annotated[
            int | None,
            Field(
                description=(
                    "Lines of source to include per result. "
                    "Default (10): function/class signature + first body lines, enough to confirm the location. "
                    "0: file path and line range only. None: full chunk (~10-20 lines). "
                    "If the snippet does not contain enough context to confirm you have the right location, "
                    "call again with max_snippet_lines=None."
                ),
                ge=0,
            ),
        ] = 10,
        content: Annotated[
            _GRAPH_CONTENT | None,
            Field(description="Content to search. Defaults to the MCP server's configured content."),
        ] = None,
    ) -> str:
        """Search once with a focused query describing what the code does or its name.

        Write queries using function/class names or behavior descriptions, not error messages.
        Returns file paths and line numbers — navigate directly there, do not repeat the search.
        Pass a git URL or local path as `repo`; indexes are cached for the session.
        """
        selected = _resolve_content_selection(content, default_content)
        try:
            value = await asyncio.to_thread(run_search, repo, query, top_k, max_snippet_lines, selected)
        except Exception as exc:
            return _repo_error(repo, exc)
        return json.dumps(value)

    @server.tool()
    async def find_related(
        file_path: Annotated[
            str,
            Field(description="Path to the file as stored in the index (use file_path from a search result)."),
        ],
        line: Annotated[int, Field(description="Line number (1-indexed).")],
        repo: Annotated[str | list[str], Field(description=_REPO_DESCRIPTION)],
        top_k: Annotated[int, Field(description="Number of similar chunks to return.", ge=1)] = 5,
        max_snippet_lines: Annotated[
            int | None,
            Field(
                description=(
                    "Lines of source per result. "
                    "Default 10 = signature + first body lines. 0 = location only. None = full chunk."
                ),
                ge=0,
            ),
        ] = 10,
        content: Annotated[
            _GRAPH_CONTENT | None,
            Field(description="Content containing the related file. Defaults to the MCP server configuration."),
        ] = None,
    ) -> str:
        """Find code similar to a known location.

        Useful for discovering all implementations of an interface, all callers of a function,
        or all tests for a class. Use after `search` when you need related code beyond the primary result.
        Pass `file_path` and `line` from a prior search result.
        """
        selected = _resolve_content_selection(content, default_content)
        try:
            value = await asyncio.to_thread(run_find_related, repo, file_path, line, top_k, max_snippet_lines, selected)
        except Exception as exc:
            return _repo_error(repo, exc)
        if value is None:
            return (
                f"No chunk found at {file_path}:{line}. "
                "Make sure the file is indexed and the line number is within a known chunk."
            )
        return json.dumps(value)

    return server


def serve(content: Sequence[str] = ("code",)) -> None:
    """Run the MCP server over stdio."""
    server = create_server(default_content=content)
    asyncio.run(server.run_stdio_async())
