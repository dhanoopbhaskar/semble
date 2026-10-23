"""MCP server exposing graph_* tools, optionally alongside Semble search."""

import asyncio
import json
import logging
from typing import Annotated, Any

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
from semble_graph.search import get_semble_index
from semble_graph.search import graph_search as run_graph_search
from semble_graph.version import __version__

logger = logging.getLogger(__name__)

_REPO_DESCRIPTION = (
    "Local repository path to graph (e.g. /path/to/repo). The sidecar builds or loads its own cache; "
    "Semble's cache is never modified."
)


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


def create_server(include_search: bool = False) -> Any:
    """Create the sidecar MCP server."""
    from mcp.server.mcpserver import MCPServer
    from pydantic import Field

    server = MCPServer(
        "semble-graph",
        version=__version__,
        instructions=(
            "Deterministic code graph for local repositories. Use `search` for text retrieval, then "
            "`graph_symbol` for exact definitions, `graph_neighbors` for callers/callees/importers, "
            "`graph_impact` for blast radius, and `graph_overview` for repository orientation. "
            "Every edge includes a confidence label and evidence line. Pass the repository root as `repo`."
        ),
    )

    @server.tool()
    def graph_symbol(
        name: Annotated[str, Field(description="Symbol name or qualified name, e.g. PaymentService or Class.method.")],
        repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
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
        repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
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
        repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
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
        repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
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
        repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
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
        repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
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

    if include_search:

        @server.tool()
        def search(
            query: Annotated[str, Field(description="Natural language or code query.")],
            repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
            top_k: Annotated[int, Field(description="Number of results to return.", ge=1)] = 5,
        ) -> str:
            """Search a repository using Semble's library (combined mode)."""
            try:
                index = get_semble_index(repo)
                results = compat.search(index, query, top_k)
                return _json(
                    {
                        "query": query,
                        "results": [
                            {
                                "file_path": result.chunk.file_path,
                                "start_line": result.chunk.start_line,
                                "end_line": result.chunk.end_line,
                                "score": result.score,
                                "snippet": result.chunk.content,
                            }
                            for result in results
                        ],
                    }
                )
            except Exception as exc:
                return _error(exc)

        @server.tool()
        def find_related(
            file_path: Annotated[str, Field(description="Repository-relative file path.")],
            line: Annotated[int, Field(description="1-indexed line number.", ge=1)],
            repo: Annotated[str, Field(description=_REPO_DESCRIPTION)],
            top_k: Annotated[int, Field(description="Number of results to return.", ge=1)] = 5,
        ) -> str:
            """Find Semble chunks related to a known file and line (combined mode)."""
            try:
                index = get_semble_index(repo)
                results = compat.find_related(index, file_path, line, top_k)
                return _json(
                    {
                        "file_path": file_path,
                        "line": line,
                        "results": [
                            {
                                "file_path": result.chunk.file_path,
                                "start_line": result.chunk.start_line,
                                "end_line": result.chunk.end_line,
                                "score": result.score,
                                "snippet": result.chunk.content,
                            }
                            for result in results
                        ],
                    }
                )
            except Exception as exc:
                return _error(exc)

    return server


def serve(include_search: bool = False) -> None:
    """Run the MCP server over stdio."""
    server = create_server(include_search=include_search)
    asyncio.run(server.run_stdio_async())
