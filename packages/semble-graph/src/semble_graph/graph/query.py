"""Graph query surface: symbols, neighbours, impact, overview, and paths."""

from __future__ import annotations

import logging
import posixpath
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from semble_graph.cache import get_cache_layout
from semble_graph.config import Settings
from semble_graph.graph.build import build_graph
from semble_graph.graph.freshness import is_fresh, repo_fingerprint, should_rebuild_in_background
from semble_graph.graph.model import Edge, Graph, Symbol, SymbolKind
from semble_graph.graph.rank import edge_label
from semble_graph.graph.store import load_graph
from semble_graph.graph.walker import LANGUAGES, walk_repo

logger = logging.getLogger(__name__)

_DEFAULT_RELATIONS = (
    "calls",
    "imports",
    "imports_from",
    "inherits",
    "implements",
    "tests",
    "references",
)
_CONTAINMENT = {"defines", "contains"}
_RELATION_ALIASES: dict[str, tuple[str, str]] = {
    "callers": ("in", "calls"),
    "caller": ("in", "calls"),
    "callees": ("out", "calls"),
    "callee": ("out", "calls"),
    "importers": ("in", "imports"),
    "importer": ("in", "imports"),
    "imports": ("out", "imports"),
    "inherits": ("out", "inherits"),
    "bases": ("out", "inherits"),
    "subclasses": ("in", "inherits"),
    "implements": ("out", "implements"),
    "implemented_by": ("in", "implements"),
    "tests": ("in", "tests"),
    "tested_by": ("in", "tests"),
    "tests_this": ("out", "tests"),
}

_CACHE_LOCK = threading.Lock()
_GRAPH_CACHE: dict[str, tuple[float, Graph]] = {}
_SERVICE_CACHE: dict[str, "GraphService"] = {}
_REBUILDING: set[str] = set()
_MAX_RELATION_DEPTH = 8


@dataclass(slots=True)
class LoadedGraph:
    """A graph plus its freshness status."""

    graph: Graph
    status: str
    cache_key: str
    cache_path: str


def _cache_key(repo: Path, settings: Settings) -> str:
    return str(get_cache_layout(repo, settings).key_dir.name)


def clear_cache() -> None:
    """Drop the in-process graph and service caches (used by tests)."""
    with _CACHE_LOCK:
        _GRAPH_CACHE.clear()
        _SERVICE_CACHE.clear()


def _schedule_rebuild(repo: Path, settings: Settings) -> None:
    key = _cache_key(repo, settings)
    with _CACHE_LOCK:
        if key in _REBUILDING:
            return
        _REBUILDING.add(key)

    def _run() -> None:
        try:
            result = build_graph(repo, settings)
            with _CACHE_LOCK:
                _GRAPH_CACHE[key] = (time.monotonic(), result.graph)
        except Exception:
            logger.exception("Background graph rebuild failed for %s", repo)
        finally:
            with _CACHE_LOCK:
                _REBUILDING.discard(key)

    thread = threading.Thread(target=_run, name=f"semble-graph-rebuild-{key}", daemon=True)
    thread.start()


class GraphService:
    """Load, validate, and (when needed) rebuild a repository graph."""

    def __init__(self, repo: str | Path, settings: Settings | None = None) -> None:
        """Create a service for one repository."""
        self.repo = Path(repo).expanduser().resolve()
        self.settings = settings or Settings.from_env()
        self.layout = get_cache_layout(self.repo, self.settings)
        self.key = self.layout.key_dir.name
        self._last_check = 0.0
        self._status = "fresh"
        self._graph: Graph | None = None

    def _cooldown_seconds(self, graph: Graph) -> float:
        """Return the revalidation cooldown for a graph.

        Mirrors Semble's adaptive policy: wait at least ``factor`` times the last
        build's duration, with a small configured floor for very fast builds.
        """
        raw = graph.meta.get("build_seconds")
        build_seconds = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
        return max(self.settings.revalidate_seconds, max(0.0, build_seconds) * self.settings.revalidate_factor)

    def load(self, rebuild: bool = True, background: bool = True) -> LoadedGraph | None:
        """Return a graph, rebuilding synchronously when stale unless the repo is large."""
        cached = self._cached()
        if cached is None:
            if not rebuild:
                return None
            try:
                result = build_graph(self.repo, self.settings)
            except Exception:
                logger.exception("Graph build failed for %s", self.repo)
                return None
            self._remember(result.graph)
            self._graph = result.graph
            self._last_check = time.monotonic()
            self._status = "fresh"
            return LoadedGraph(result.graph, "fresh", self.key, str(self.layout.graph_dir))

        if self._graph is not None and cached is not self._graph:
            # A background rebuild replaced the cached graph; the new graph is fresh.
            self._status = "fresh"
            self._last_check = time.monotonic()
        self._graph = cached

        now = time.monotonic()
        if now - self._last_check < self._cooldown_seconds(cached):
            return LoadedGraph(cached, self._status, self.key, str(self.layout.graph_dir))

        self._last_check = now
        meta = cached.meta
        try:
            walk = walk_repo(self.repo, self.settings)
            fingerprint = repo_fingerprint(self.repo, walk)
        except Exception:
            logger.exception("Could not compute repository fingerprint for %s", self.repo)
            return LoadedGraph(cached, "unknown", self.key, str(self.layout.graph_dir))

        if is_fresh(meta, fingerprint):
            self._status = "fresh"
            return LoadedGraph(cached, "fresh", self.key, str(self.layout.graph_dir))

        if not rebuild:
            self._status = "stale"
            return LoadedGraph(cached, "stale", self.key, str(self.layout.graph_dir))

        if background and should_rebuild_in_background(len(walk.files), self.settings):
            self._status = "stale"
            _schedule_rebuild(self.repo, self.settings)
            return LoadedGraph(cached, "stale", self.key, str(self.layout.graph_dir))

        try:
            result = build_graph(self.repo, self.settings)
        except Exception:
            logger.exception("Synchronous graph rebuild failed for %s", self.repo)
            self._status = "stale"
            return LoadedGraph(cached, "stale", self.key, str(self.layout.graph_dir))
        self._remember(result.graph)
        self._graph = result.graph
        self._last_check = time.monotonic()
        self._status = "fresh"
        return LoadedGraph(result.graph, "rebuilt", self.key, str(self.layout.graph_dir))

    def _cached(self) -> Graph | None:
        with _CACHE_LOCK:
            hit = _GRAPH_CACHE.get(self.key)
        if hit is not None:
            return hit[1]
        graph = load_graph(self.layout)
        if graph is None:
            return None
        with _CACHE_LOCK:
            _GRAPH_CACHE[self.key] = (time.monotonic(), graph)
        return graph

    def _remember(self, graph: Graph) -> None:
        with _CACHE_LOCK:
            _GRAPH_CACHE[self.key] = (time.monotonic(), graph)


def get_service(repo: str | Path, settings: Settings | None = None) -> GraphService:
    """Return a process-cached service so freshness checks are not repeated per call."""
    settings = settings or Settings.from_env()
    resolved = Path(repo).expanduser().resolve()
    key = f"{resolved}|{settings.cache_dir}"
    with _CACHE_LOCK:
        cached = _SERVICE_CACHE.get(key)
    if cached is not None:
        return cached
    service = GraphService(resolved, settings)
    with _CACHE_LOCK:
        _SERVICE_CACHE[key] = service
    return service


# ----------------------------------------------------------------------
# Public query functions
# ----------------------------------------------------------------------


def resolve_symbols(
    graph: Graph,
    name: str,
    kind: str | SymbolKind | None = None,
    *,
    limit: int = 25,
) -> list[Symbol]:
    """Resolve a name to graph symbols, preferring exact and qualified matches."""
    query = name.strip()
    if not query:
        return []
    wanted_kind: SymbolKind | None = None
    if kind is not None:
        try:
            wanted_kind = kind if isinstance(kind, SymbolKind) else SymbolKind(kind)
        except ValueError:
            wanted_kind = None

    lowered = query.lower()
    scored: list[tuple[int, Symbol]] = []

    def _score(symbol: Symbol) -> int | None:
        if wanted_kind is not None and symbol.kind is not wanted_kind:
            return None
        if symbol.kind is SymbolKind.FILE and "/" not in query and "\\" not in query:
            return None
        if symbol.qualified_name == query:
            return 0
        if symbol.qualified_name.lower() == lowered:
            return 1
        if symbol.name == query:
            return 2
        if symbol.name.lower() == lowered:
            return 3
        if symbol.qualified_name.lower().endswith(f"::{lowered}") or symbol.qualified_name.lower().endswith(
            f".{lowered}"
        ):
            return 4
        return None

    for symbol in graph.symbols_named(query):
        score = _score(symbol)
        if score is not None:
            scored.append((score, symbol))
    if not scored:
        for symbol in graph.symbols:
            score = _score(symbol)
            if score is not None:
                scored.append((score, symbol))
    scored.sort(key=lambda item: (item[0], item[1].file_path, item[1].start_line))
    return [symbol for _, symbol in scored[:limit]]


def symbol_tiny(symbol: Symbol) -> dict[str, Any]:
    """Serialize a compact symbol reference."""
    return {
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind.value,
        "file_path": symbol.file_path,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
    }


def symbol_payload(symbol: Symbol) -> dict[str, Any]:
    """Serialize a full symbol definition."""
    payload = symbol_tiny(symbol)
    payload["location"] = f"{symbol.file_path}:{symbol.start_line}-{symbol.end_line}"
    return payload


def _symbol_snippet(repo: str | Path | None, symbol: Symbol, max_lines: int) -> str | None:
    """Read a bounded source snippet scoped to a symbol's own span."""
    if repo is None or max_lines <= 0:
        return None
    path = Path(repo).expanduser()
    try:
        with (path / symbol.file_path).open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    start = max(1, symbol.start_line)
    end = min(len(lines), symbol.end_line, start + max_lines - 1)
    if end < start:
        return None
    return "".join(lines[start - 1 : end]).rstrip()


def _edge_payload(graph: Graph, edge: Edge, direction: str, depth: int | None = None) -> dict[str, Any]:
    other_id = edge.src if direction == "in" else edge.dst
    other = graph.symbol(other_id)
    payload = {
        "relation": edge_label(edge, direction),
        "edge": edge.kind,
        "direction": direction,
        "confidence": edge.confidence,
        "line": edge.line,
        "symbol": symbol_tiny(other) if other is not None else None,
    }
    if depth is not None:
        payload["depth"] = depth
    return payload


def _closure_payload(graph: Graph, edge: Edge, relation: str, depth: int, direct: bool) -> dict[str, Any]:
    """Serialize one edge in a transitive relation closure."""
    payload = _edge_payload(graph, edge, "in", depth)
    payload["relation"] = relation
    payload["direct"] = direct
    return payload


def _implementations(graph: Graph, symbol: Symbol, depth: int) -> list[dict[str, Any]]:
    """Return direct and indirect implementers of ``symbol``.

    Direct implementers are reached through incoming ``implements`` edges. Their
    subclasses (for example a concrete class extending an abstract implementation)
    are reported as indirect implementers. Interfaces that extend ``symbol`` are
    traversed so implementations of a sub-interface are still discovered.
    """
    items: list[dict[str, Any]] = []
    seen = {symbol.id}
    frontier: deque[tuple[int, int]] = deque([(symbol.id, 0)])
    walk_interface_hierarchy = symbol.kind is SymbolKind.TYPE
    while frontier:
        current, current_depth = frontier.popleft()
        if current_depth >= depth:
            continue
        for edge in graph.in_edges(current, {"implements", "inherits"}):
            if edge.src in seen:
                continue
            direct = current_depth == 0 and edge.kind == "implements"
            if current_depth == 0 and edge.kind == "inherits":
                # An outgoing ``inherits`` edge from an interface points at a
                # sub-interface: traverse it but do not count it as an implementation.
                if not walk_interface_hierarchy:
                    continue
                seen.add(edge.src)
                frontier.append((edge.src, current_depth + 1))
                continue
            seen.add(edge.src)
            items.append(_closure_payload(graph, edge, "implemented_by", current_depth + 1, direct))
            frontier.append((edge.src, current_depth + 1))
    return items


def _subclasses(graph: Graph, symbol: Symbol, depth: int) -> list[dict[str, Any]]:
    """Return direct and indirect subclasses of ``symbol``."""
    items: list[dict[str, Any]] = []
    seen = {symbol.id}
    frontier: deque[tuple[int, int]] = deque([(symbol.id, 0)])
    while frontier:
        current, current_depth = frontier.popleft()
        if current_depth >= depth:
            continue
        for edge in graph.in_edges(current, {"inherits"}):
            if edge.src in seen:
                continue
            seen.add(edge.src)
            items.append(_closure_payload(graph, edge, "inherited_by", current_depth + 1, current_depth == 0))
            frontier.append((edge.src, current_depth + 1))
    return items


def _relations(graph: Graph, symbol: Symbol, depth: int = 2) -> dict[str, list[dict[str, Any]]]:
    """Return relations for a symbol, grouped by role.

    ``implemented_by`` and ``inherited_by`` include transitive results up to
    ``depth`` and tag each entry with ``direct`` and ``depth``.
    """
    depth = max(1, min(depth, _MAX_RELATION_DEPTH))
    callers = [_edge_payload(graph, edge, "in") for edge in graph.in_edges(symbol.id, {"calls"})]
    callees = [_edge_payload(graph, edge, "out") for edge in graph.out_edges(symbol.id, {"calls"})]
    importers = [_edge_payload(graph, edge, "in") for edge in graph.in_edges(symbol.id, {"imports", "imports_from"})]
    imports = [_edge_payload(graph, edge, "out") for edge in graph.out_edges(symbol.id, {"imports", "imports_from"})]
    inherits = [_edge_payload(graph, edge, "out") for edge in graph.out_edges(symbol.id, {"inherits"})]
    inherited_by = _subclasses(graph, symbol, depth)
    implements = [_edge_payload(graph, edge, "out") for edge in graph.out_edges(symbol.id, {"implements"})]
    implemented_by = _implementations(graph, symbol, depth)
    tested_by = [_edge_payload(graph, edge, "in") for edge in graph.in_edges(symbol.id, {"tests"})]
    tests = [_edge_payload(graph, edge, "out") for edge in graph.out_edges(symbol.id, {"tests"})]
    return {
        "callers": _sort_relations(callers),
        "callees": _sort_relations(callees),
        "importers": _sort_relations(importers),
        "imports": _sort_relations(imports),
        "inherits": _sort_relations(inherits),
        "inherited_by": _sort_relations(inherited_by),
        "implements": _sort_relations(implements),
        "implemented_by": _sort_relations(implemented_by),
        "tested_by": _sort_relations(tested_by),
        "tests": _sort_relations(tests),
    }


def _sort_relations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            item.get("depth", 0),
            (item.get("symbol") or {}).get("file_path", ""),
            (item.get("symbol") or {}).get("start_line", 0),
            item["relation"],
        ),
    )


def graph_symbol(
    graph: Graph,
    name: str,
    kind: str | None = None,
    include_relations: bool = True,
    depth: int = 2,
    repo: str | Path | None = None,
    max_snippet_lines: int | None = None,
) -> dict[str, Any]:
    """Exact/qualified symbol lookup with optional transitive relations and snippet."""
    matches = resolve_symbols(graph, name, kind)
    payloads: list[dict[str, Any]] = []
    for symbol in matches:
        payload = symbol_payload(symbol)
        if include_relations:
            payload["relations"] = _relations(graph, symbol, depth)
        if max_snippet_lines is not None:
            snippet = _symbol_snippet(repo, symbol, max_snippet_lines)
            if snippet:
                payload["snippet"] = snippet
        payloads.append(payload)
    return {"query": name, "count": len(payloads), "matches": payloads}


def _anchor(
    graph: Graph,
    symbol: str | None,
    file_path: str | None,
    line: int | None,
) -> list[Symbol]:
    if symbol:
        return resolve_symbols(graph, symbol, limit=5)
    if file_path:
        if line is not None:
            found = graph.symbol_at(file_path, line)
            return [found] if found else []
        return [item for item in graph.symbols_in_file(file_path) if item.kind is not SymbolKind.FILE]
    return []


def graph_neighbors(
    graph: Graph,
    symbol: str | None = None,
    file_path: str | None = None,
    line: int | None = None,
    relation: str | None = None,
    depth: int = 1,
    max_results: int = 250,
) -> dict[str, Any]:
    """Return neighbouring symbols grouped by traversal depth."""
    anchors = _anchor(graph, symbol, file_path, line)
    if not anchors:
        return {"error": "No symbol found for the requested position.", "anchors": [], "neighbors": []}
    depth = max(1, min(depth, 8))
    allowed, direction_filter = _parse_relation(relation)
    results: list[dict[str, Any]] = []
    visited = {anchor.id for anchor in anchors}
    frontier: deque[tuple[int, int]] = deque((anchor.id, 0) for anchor in anchors)
    while frontier and len(results) < max_results:
        current, current_depth = frontier.popleft()
        if current_depth >= depth:
            continue
        for direction, edges in (("out", graph.out_edges(current)), ("in", graph.in_edges(current))):
            if direction_filter and direction != direction_filter:
                continue
            for edge in edges:
                other_id = edge.dst if direction == "out" else edge.src
                if other_id in visited:
                    continue
                if relation is None and edge.kind in _CONTAINMENT:
                    continue
                if allowed is not None and edge.kind not in allowed:
                    continue
                visited.add(other_id)
                payload = _edge_payload(graph, edge, direction, current_depth + 1)
                payload["via"] = symbol_tiny(graph.require_symbol(current))
                results.append(payload)
                frontier.append((other_id, current_depth + 1))
                if len(results) >= max_results:
                    break
    return {
        "anchors": [symbol_payload(anchor) for anchor in anchors],
        "depth": depth,
        "relation": relation,
        "neighbors": sorted(results, key=lambda item: (item["depth"], item["relation"])),
    }


def _parse_relation(relation: str | None) -> tuple[set[str] | None, str | None]:
    if not relation or relation == "all":
        return None, None
    relation = relation.strip().lower()
    if relation in _RELATION_ALIASES:
        direction, kind = _RELATION_ALIASES[relation]
        return {kind}, direction
    return {relation}, None


def graph_impact(
    graph: Graph,
    paths: list[str] | None = None,
    symbols: list[str] | None = None,
    depth: int = 2,
    relations: list[str] | None = None,
    max_results: int = 500,
) -> dict[str, Any]:
    """Return reverse-reachable dependants grouped by depth."""
    depth = max(1, min(depth, 8))
    seeds: list[Symbol] = []
    for name in symbols or []:
        seeds.extend(resolve_symbols(graph, name, limit=10))
    for path in paths or []:
        path = path.strip()
        if not path:
            continue
        if path.endswith("/") or path in {".", ""}:
            for symbol in graph.symbols:
                if symbol.file_path.startswith(path) and symbol.kind is not SymbolKind.FILE:
                    seeds.append(symbol)
        else:
            direct = [item for item in graph.symbols_in_file(path) if item.kind is not SymbolKind.FILE]
            if direct:
                seeds.extend(direct)
            else:
                seeds.extend(
                    symbol
                    for symbol in graph.symbols
                    if symbol.file_path.startswith(posixpath.normpath(path)) and symbol.kind is not SymbolKind.FILE
                )
    seeds = list({symbol.id: symbol for symbol in seeds}.values())
    if not seeds:
        return {"error": "No seeds found.", "seeds": [], "results": [], "total": 0}

    allowed = set(relations or _DEFAULT_RELATIONS)
    allowed.discard("defines")
    allowed.discard("contains")
    visited = {seed.id for seed in seeds}
    frontier: deque[tuple[int, int]] = deque((seed.id, 0) for seed in seeds)
    grouped: dict[int, list[dict[str, Any]]] = {}
    total = 0
    while frontier and total < max_results:
        current, current_depth = frontier.popleft()
        if current_depth >= depth:
            continue
        for edge in graph.in_edges(current, allowed):
            dependent = edge.src
            if dependent in visited:
                continue
            visited.add(dependent)
            payload = _edge_payload(graph, edge, "in", current_depth + 1)
            payload["via"] = symbol_tiny(graph.require_symbol(current))
            grouped.setdefault(current_depth + 1, []).append(payload)
            frontier.append((dependent, current_depth + 1))
            total += 1
            if total >= max_results:
                break
    results = [
        {
            "depth": level,
            "symbols": sorted(items, key=lambda item: item["symbol"]["file_path"] if item["symbol"] else ""),
        }
        for level, items in sorted(grouped.items())
    ]
    return {
        "seeds": [symbol_payload(seed) for seed in seeds[:50]],
        "depth": depth,
        "relations": sorted(allowed),
        "total": total,
        "results": results,
    }


def graph_overview(graph: Graph, top_n: int = 15) -> dict[str, Any]:
    """Return a repository overview: core symbols, entry points, and counts."""
    degrees: dict[int, int] = {}
    for edge in graph.edges:
        if edge.kind in _CONTAINMENT:
            continue
        degrees[edge.src] = degrees.get(edge.src, 0) + 1
        degrees[edge.dst] = degrees.get(edge.dst, 0) + 1

    code_symbols = [
        symbol
        for symbol in graph.symbols
        if symbol.kind not in {SymbolKind.FILE, SymbolKind.MODULE} and "test" not in symbol.file_path.lower()
    ]
    ranked_degrees = sorted(degrees.get(symbol.id, 0) for symbol in code_symbols)
    if ranked_degrees:
        hub_threshold = max(50, ranked_degrees[int(len(ranked_degrees) * 0.99)])
    else:
        hub_threshold = 50
    core = [
        symbol for symbol in code_symbols if degrees.get(symbol.id, 0) > 0 and degrees.get(symbol.id, 0) < hub_threshold
    ]
    core.sort(key=lambda symbol: (-degrees.get(symbol.id, 0), symbol.file_path, symbol.start_line))

    callers: dict[int, int] = {}
    for edge in graph.edges:
        if edge.kind == "calls":
            callers[edge.dst] = callers.get(edge.dst, 0) + 1
    entry_points = [
        symbol
        for symbol in code_symbols
        if symbol.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
        and callers.get(symbol.id, 0) == 0
        and "test" not in symbol.name.lower()
    ]
    entry_points.sort(
        key=lambda symbol: (
            0 if symbol.name in {"main", "Main", "run", "start"} else 1,
            -len(graph.out_edges(symbol.id, _DEFAULT_RELATIONS)),
            symbol.file_path,
        )
    )

    languages = graph.meta.get("languages")
    if not isinstance(languages, dict):
        languages = {}
        for symbol in graph.symbols:
            if symbol.kind is SymbolKind.FILE:
                language = LANGUAGES.get(Path(symbol.file_path).suffix.lower())
                if language:
                    languages[language] = languages.get(language, 0) + 1
    counts = graph.meta.get("counts") if isinstance(graph.meta.get("counts"), dict) else {}

    return {
        "counts": counts,
        "languages": languages,
        "core_symbols": [
            {**symbol_payload(symbol), "degree": degrees.get(symbol.id, 0)} for symbol in core[: max(1, top_n)]
        ],
        "entry_points": [symbol_payload(symbol) for symbol in entry_points[: max(1, top_n)]],
        "hub_threshold": hub_threshold,
    }


def graph_path(
    graph: Graph,
    source: str,
    target: str,
    max_hops: int = 6,
) -> dict[str, Any]:
    """Return a shortest relation path between two symbols."""
    sources = resolve_symbols(graph, source, limit=3)
    targets = resolve_symbols(graph, target, limit=3)
    if not sources or not targets:
        return {"found": False, "error": "Source or target not found.", "path": []}
    target_ids = {symbol.id for symbol in targets}
    max_hops = max(1, min(max_hops, 12))
    allowed = set(_DEFAULT_RELATIONS) | {"defines", "contains"}
    for direction_mode in ("out", "both"):
        queue: deque[tuple[int, list[int], list[Edge]]] = deque((symbol.id, [symbol.id], []) for symbol in sources)
        visited = {symbol.id for symbol in sources}
        while queue:
            current, nodes, edges = queue.popleft()
            if len(edges) >= max_hops:
                continue
            candidates: list[tuple[int, Edge, str]] = [
                (edge.dst, edge, "out") for edge in graph.out_edges(current, allowed)
            ]
            if direction_mode == "both":
                candidates.extend((edge.src, edge, "in") for edge in graph.in_edges(current, allowed))
            for next_id, edge, direction in candidates:
                if next_id in visited:
                    continue
                next_nodes = [*nodes, next_id]
                next_edges = [*edges, edge]
                if next_id in target_ids:
                    return {
                        "found": True,
                        "hops": len(next_edges),
                        "direction": direction_mode,
                        "path": [symbol_tiny(graph.require_symbol(item)) for item in next_nodes],
                        "edges": [
                            {
                                "relation": edge_label(item, "out" if item.src == next_nodes[index] else "in"),
                                "kind": item.kind,
                                "confidence": item.confidence,
                                "line": item.line,
                            }
                            for index, item in enumerate(next_edges)
                        ],
                    }
                visited.add(next_id)
                queue.append((next_id, next_nodes, next_edges))
    return {"found": False, "path": [], "edges": [], "max_hops": max_hops}
