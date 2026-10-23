"""Graph-aware search: sidecar-only augmentation of Semble retrieval."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from semble_graph import compat
from semble_graph.config import Settings
from semble_graph.graph.model import Graph, Symbol, SymbolKind
from semble_graph.graph.query import (
    get_service,
    graph_impact,
    graph_neighbors,
    resolve_symbols,
    symbol_payload,
)
from semble_graph.graph.rank import (
    Seed,
    co_location_bonus,
    exactness_bonus,
    expand_seeds,
    normalize_scores,
    proximity_bonus,
    test_bonus,
)

logger = logging.getLogger(__name__)

_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: dict[str, Any] = {}
_MERGED_LOCK = threading.Lock()
_MERGED: tuple[list[Any], Any] | None = None
_RELATIONSHIP = re.compile(
    r"\b(who\s+calls?|callers?|callees?|what\s+imports?|importers?|impact|blast(?:\s+radius)?|"
    r"depends?\s+on|tested\s+by|tests?|inherits?|implements?|subclasses?|neighbors?|neighbours?)\b",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:(?:::|\.)[A-Za-z_$][A-Za-z0-9_$]*)*")
_SYMBOL_QUERY = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:(?:::|\.)[A-Za-z_$][A-Za-z0-9_$]*)*$")
_TEST_QUERY = re.compile(r"\b(tests?|tested|specs?)\b", re.IGNORECASE)


@dataclass(slots=True)
class Candidate:
    """A ranked output candidate."""

    symbol: Symbol
    score: float
    via: str
    chunk: Any | None = None
    relation: str | None = None
    confidence: str | None = None
    evidence_line: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _normalize_source(source: str | Path) -> str:
    """Return a stable cache key: resolved path for local dirs, URL untouched for git sources."""
    raw = str(source)
    return raw if compat.is_git_url(raw) else str(Path(raw).expanduser().resolve())


def _get_index(source: str | Path, content: tuple[str, ...] = ("code",)) -> Any:
    """Return a cached Semble index for one local path or git URL."""
    raw = str(source)
    key = f"{_normalize_source(raw)}:{','.join(content)}"
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index = compat.build_index(raw, content)
    with _INDEX_LOCK:
        if len(_INDEX_CACHE) >= 4:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
        _INDEX_CACHE[key] = index
    return index


def _validate_sources(repos: Sequence[str]) -> list[str]:
    """Deduplicate sources and reject unsafe git transports, mirroring Semble."""
    sources = list(dict.fromkeys(str(repo) for repo in repos))
    if not sources:
        raise ValueError("`repo` must name at least one local path or git URL.")
    for source in sources:
        if compat.is_git_url(source) and not source.startswith(("https://", "http://")):
            raise ValueError(
                f"Only https://, http://, or local directory paths are accepted as `repo`. Got: {source!r}"
            )
    return sources


def get_semble_indexes(repos: str | Sequence[str], content: tuple[str, ...] = ("code",)) -> Any:
    """Return a cached Semble index for one or several repos (merged when needed)."""
    sources = _validate_sources([repos] if isinstance(repos, (str, Path)) else repos)
    parts = [(source, _get_index(source, content)) for source in sources]
    if len(parts) == 1:
        return parts[0][1]

    global _MERGED
    indexes = [index for _, index in parts]
    with _MERGED_LOCK:
        if _MERGED is None or _MERGED[0] != indexes:
            _MERGED = (indexes, compat.merge_indexes(parts))
        return _MERGED[1]


def get_semble_index(repo: str | Path, content: tuple[str, ...] = ("code",)) -> Any:
    """Return a cached Semble index for a repository."""
    return _get_index(repo, content)


def clear_index_cache() -> None:
    """Drop the in-process Semble index cache (used by tests)."""
    global _MERGED
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
    with _MERGED_LOCK:
        _MERGED = None


def _map_chunk(graph: Graph, chunk: Any) -> Symbol | None:
    """Map a Semble chunk to the graph symbol with the largest line overlap."""
    file_path = getattr(chunk, "file_path", "")
    start = getattr(chunk, "start_line", 1)
    end = getattr(chunk, "end_line", start)
    best: Symbol | None = None
    best_overlap = 0
    for symbol in graph.symbols_in_file(file_path):
        if symbol.kind is SymbolKind.FILE:
            continue
        overlap = max(0, min(end, symbol.end_line) - max(start, symbol.start_line) + 1)
        if overlap > best_overlap:
            best = symbol
            best_overlap = overlap
    return best or graph.file_symbol(file_path)


def _snippet_for_symbol(repo: Path, symbol: Symbol, max_lines: int = 40) -> str:
    """Read a bounded source snippet for a symbol."""
    try:
        with (repo / symbol.file_path).open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    start = max(1, symbol.start_line)
    end = min(len(lines), symbol.end_line, start + max_lines - 1)
    return "".join(lines[start - 1 : end]).rstrip()


def _query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    for match in _IDENTIFIER.finditer(query):
        token = match.group(0)
        if len(token) >= 3:
            tokens.append(token)
        if "::" in token or "." in token:
            tokens.append(token.replace("::", ".").rsplit(".", 1)[-1])
    return list(dict.fromkeys(tokens))


def _exact_symbols(graph: Graph, query: str) -> list[Symbol]:
    matches = resolve_symbols(graph, query.strip(), limit=10)
    exact = [symbol for symbol in matches if symbol.name.lower() == query.strip().lower()]
    if exact:
        return exact
    results: list[Symbol] = []
    for token in _query_tokens(query):
        results.extend(resolve_symbols(graph, token, limit=5))
    unique = {symbol.id: symbol for symbol in results}
    return sorted(unique.values(), key=lambda symbol: (symbol.file_path, symbol.start_line))[:10]


def _relationship_candidates(graph: Graph, query: str, anchors: list[Symbol], repo: Path) -> list[Candidate]:
    """Build candidates from graph traversal for relationship queries."""
    if not anchors:
        return []
    lowered = query.lower()
    candidates: list[Candidate] = []
    if any(word in lowered for word in ("impact", "blast", "depends on", "dependency")):
        result = graph_impact(graph, symbols=[anchor.name for anchor in anchors], depth=2)
    elif "import" in lowered:
        result = graph_neighbors(graph, symbol=anchors[0].name, relation="importers", depth=1)
    elif "test" in lowered:
        result = graph_neighbors(graph, symbol=anchors[0].name, relation="tested_by", depth=1)
    elif any(word in lowered for word in ("inherit", "subclass", "implement")):
        result = graph_neighbors(graph, symbol=anchors[0].name, depth=1)
    else:
        result = graph_neighbors(graph, symbol=anchors[0].name, relation="callers", depth=1)

    if "results" in result:
        for group in result.get("results", []):
            for item in group.get("symbols", []):
                symbol_data = item.get("symbol")
                if not symbol_data:
                    continue
                symbol = graph.symbol(int(symbol_data["id"])) if "id" in symbol_data else None
                if symbol is None:
                    matches = resolve_symbols(graph, symbol_data["qualified_name"], limit=1)
                    symbol = matches[0] if matches else None
                if symbol is None:
                    continue
                candidates.append(
                    Candidate(
                        symbol=symbol,
                        score=max(0.65, 1.05 - 0.12 * int(group.get("depth", 1))),
                        via="graph",
                        relation=item.get("relation"),
                        confidence=item.get("confidence"),
                        evidence_line=item.get("line"),
                    )
                )
    else:
        for item in result.get("neighbors", []):
            symbol_data = item.get("symbol")
            if not symbol_data:
                continue
            matches = resolve_symbols(graph, symbol_data["qualified_name"], limit=1)
            if not matches:
                continue
            candidates.append(
                Candidate(
                    symbol=matches[0],
                    score=max(0.65, 1.05 - 0.12 * int(item.get("depth", 1))),
                    via="graph",
                    relation=item.get("relation"),
                    confidence=item.get("confidence"),
                    evidence_line=item.get("line"),
                )
            )
    return candidates


def _token_count(text: str) -> int:
    return max(1, len(text) // 4)


def _pack(candidates: list[Candidate], repo: Path, top_k: int, token_budget: int | None) -> list[dict[str, Any]]:
    """Sort, dedupe, enforce diversity, and pack snippets to a token budget."""
    candidates.sort(key=lambda item: (-item.score, item.symbol.file_path, item.symbol.start_line))
    seen_symbols: set[int] = set()
    per_file: dict[str, int] = {}
    packed: list[dict[str, Any]] = []
    used_tokens = 0
    for candidate in candidates:
        if candidate.symbol.id in seen_symbols:
            continue
        if len(packed) >= top_k:
            break
        file_count = per_file.get(candidate.symbol.file_path, 0)
        if file_count >= 2 and len(packed) >= 3:
            continue
        snippet = _snippet_for_symbol(repo, candidate.symbol)
        if candidate.chunk is not None:
            chunk_content = getattr(candidate.chunk, "content", "")
            if chunk_content:
                snippet = chunk_content.rstrip()
        snippet_lines = snippet.splitlines()
        if len(snippet_lines) > 40:
            snippet = "\n".join(snippet_lines[:40])
        tokens = _token_count(snippet)
        if token_budget is not None and used_tokens + tokens > token_budget:
            remaining = token_budget - used_tokens
            if remaining < 40:
                break
            snippet = snippet[: remaining * 4]
            tokens = _token_count(snippet)
        seen_symbols.add(candidate.symbol.id)
        per_file[candidate.symbol.file_path] = file_count + 1
        used_tokens += tokens
        payload = symbol_payload(candidate.symbol)
        payload.update(
            {
                "score": round(candidate.score, 4),
                "via": candidate.via,
                "relation": candidate.relation,
                "confidence": candidate.confidence,
                "evidence_line": candidate.evidence_line,
                "snippet": snippet,
            }
        )
        packed.append(payload)
    return packed


def _plain_results(results: list[Any], repo: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for result in results:
        chunk = getattr(result, "chunk", None)
        if chunk is None:
            continue
        payloads.append(
            {
                "file_path": chunk.file_path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "language": getattr(chunk, "language", None),
                "score": round(float(getattr(result, "score", 0.0)), 4),
                "snippet": getattr(chunk, "content", ""),
            }
        )
    return payloads


def graph_search(
    repo: str | Path,
    query: str,
    top_k: int = 10,
    token_budget: int | None = None,
    expand: bool = True,
    settings: Settings | None = None,
    content: tuple[str, ...] = ("code",),
) -> dict[str, Any]:
    """Run Semble retrieval and augment it with graph context.

    Fails open: when the graph is unavailable, plain Semble results are returned
    with ``"graph_status": "unavailable"``.
    """
    repo_path = Path(repo).expanduser().resolve()
    settings = settings or Settings.from_env()
    loaded = get_service(repo_path, settings).load()
    graph = loaded.graph if loaded is not None else None
    graph_status = loaded.status if loaded is not None else "unavailable"

    if not compat.semble_available():
        return {
            "query": query,
            "graph_status": graph_status,
            "error": compat.fallback_message("Semble is not installed; install the `search` extra"),
            "results": [],
        }

    try:
        index = _get_index(str(repo_path), content)
        raw_results = compat.search(index, query, max(top_k * 4, top_k))
    except Exception as exc:
        logger.exception("Semble search failed")
        return {
            "query": query,
            "graph_status": graph_status,
            "error": compat.fallback_message(f"Semble search failed: {exc}"),
            "results": [],
        }

    if graph is None:
        return {
            "query": query,
            "graph_status": "unavailable",
            "intent": "conceptual",
            "results": _plain_results(raw_results, repo_path),
        }

    try:
        return _augment(graph, raw_results, query, repo_path, top_k, token_budget, expand, graph_status)
    except Exception as exc:
        logger.exception("Graph augmentation failed; returning plain Semble results")
        return {
            "query": query,
            "graph_status": "unavailable",
            "intent": "conceptual",
            "warning": f"Graph augmentation failed: {exc}",
            "results": _plain_results(raw_results, repo_path),
        }


def _augment(
    graph: Graph,
    raw_results: list[Any],
    query: str,
    repo_path: Path,
    top_k: int,
    token_budget: int | None,
    expand: bool,
    graph_status: str,
) -> dict[str, Any]:
    """Augment Semble results with exact symbols, traversal, and graph proximity."""
    normalized = normalize_scores([float(getattr(result, "score", 0.0)) for result in raw_results])
    seeds: list[Seed] = []
    candidates: list[Candidate] = []
    seed_files: set[str] = set()
    for rank, (result, base) in enumerate(zip(raw_results, normalized)):
        chunk = getattr(result, "chunk", None)
        if chunk is None:
            continue
        symbol = _map_chunk(graph, chunk)
        if symbol is None:
            continue
        seeds.append(Seed(symbol=symbol, base_score=base, rank=rank, chunk=chunk))
        seed_files.add(symbol.file_path)
        candidates.append(Candidate(symbol=symbol, score=base, via="semantic", chunk=chunk))

    exact = _exact_symbols(graph, query)
    relationship = bool(_RELATIONSHIP.search(query))
    wants_tests = bool(_TEST_QUERY.search(query))
    symbol_query = bool(_SYMBOL_QUERY.match(query.strip()))
    intent = "relationship" if relationship and exact else ("symbol" if symbol_query and exact else "conceptual")

    if intent == "relationship":
        candidates.extend(_relationship_candidates(graph, query, exact, repo_path))
        for candidate in candidates:
            if candidate.via == "semantic":
                candidate.score *= 0.72
    if intent in {"symbol", "relationship"}:
        for symbol in exact:
            candidates.append(Candidate(symbol=symbol, score=1.0 if intent == "symbol" else 0.98, via="exact"))

    expansions = expand_seeds(graph, seeds, max_distance=1) if expand and seeds else {}
    for symbol_id, expansion in expansions.items():
        symbol = graph.symbol(symbol_id)
        if symbol is None:
            continue
        candidates.append(
            Candidate(
                symbol=symbol,
                score=0.30 + proximity_bonus(expansion),
                via="expansion",
                relation=expansion.relation,
                confidence=expansion.confidence,
            )
        )

    for candidate in candidates:
        candidate.score += exactness_bonus(candidate.symbol, query.strip().lower())
        candidate.score += test_bonus(candidate.symbol, wants_tests)
        if candidate.via == "semantic":
            candidate.score += co_location_bonus(candidate.symbol, seed_files)
            candidate_expansion = expansions.get(candidate.symbol.id)
            candidate.score += proximity_bonus(candidate_expansion)

    packed = _pack(candidates, repo_path, top_k, token_budget)
    return {
        "query": query,
        "graph_status": graph_status,
        "intent": intent,
        "results": packed,
        "count": len(packed),
    }


def run_search(
    repos: str | Sequence[str],
    query: str,
    top_k: int = 5,
    max_snippet_lines: int | None = None,
    content: tuple[str, ...] = ("code",),
) -> dict[str, Any]:
    """Search one or more repos and return Semble's exact result payload.

    Raises when Semble is unavailable or the index cannot be built. Tool and CLI
    adapters turn that into Semble-compatible error text.
    """
    if not compat.semble_available():
        raise RuntimeError("Semble is not installed; install the `search` extra")
    index = get_semble_indexes(repos, content)
    results = compat.search(index, query, top_k, max_snippet_lines)
    if not results:
        return {"error": "No results found."}
    return compat.format_results(query, results, max_snippet_lines, index.sources)


def run_find_related(
    repos: str | Sequence[str],
    file_path: str,
    line: int,
    top_k: int = 5,
    max_snippet_lines: int | None = None,
    content: tuple[str, ...] = ("code",),
) -> dict[str, Any] | None:
    """Return Semble's exact ``find_related`` payload, or ``None`` when no chunk covers the line."""
    if not compat.semble_available():
        raise RuntimeError("Semble is not installed; install the `search` extra")
    index = get_semble_indexes(repos, content)
    if compat.resolve_chunk(index.chunks, file_path, line) is None:
        return None
    results = compat.find_related(index, file_path, line, top_k, max_snippet_lines)
    label = f"Chunks related to {file_path}:{line}"
    if not results:
        return {"error": f"No related chunks found for {file_path}:{line}."}
    return compat.format_results(label, results, max_snippet_lines, index.sources)


def plain_search(
    repo: str | Path,
    query: str,
    top_k: int = 5,
    max_snippet_lines: int | None = None,
    content: tuple[str, ...] = ("code",),
) -> dict[str, Any]:
    """Fail-open wrapper around :func:`run_search` (kept for callers that must not raise)."""
    try:
        return run_search(str(repo), query, top_k=top_k, max_snippet_lines=max_snippet_lines, content=content)
    except Exception as exc:
        logger.exception("Plain Semble search failed")
        return {
            "query": query,
            "graph_status": "unavailable",
            "error": compat.fallback_message(str(exc)),
            "results": [],
        }
