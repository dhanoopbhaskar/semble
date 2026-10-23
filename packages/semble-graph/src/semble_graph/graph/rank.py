"""Ranking helpers for graph-aware search."""

from __future__ import annotations

from dataclasses import dataclass

from semble_graph.graph.model import Edge, Graph, Symbol, SymbolKind

_HIGH_VALUE_RELATIONS = {"calls", "implements", "imports", "imports_from", "tests", "inherits"}


@dataclass(slots=True)
class Seed:
    """A retrieval seed mapped to a graph symbol."""

    symbol: Symbol
    base_score: float
    rank: int
    chunk: object | None = None


@dataclass(slots=True)
class Expansion:
    """Reachability metadata for a candidate symbol."""

    symbol_id: int
    distance: int
    via: int
    relation: str
    confidence: str


def normalize_scores(scores: list[float]) -> list[float]:
    """Normalize retrieval scores into 0..1 while preserving order."""
    if not scores:
        return []
    finite = [score for score in scores if score == score]
    maximum = max(finite) if finite else 0.0
    if maximum <= 0:
        return [1.0 / (index + 1) for index in range(len(scores))]
    return [max(0.0, score) / maximum for score in scores]


def expand_seeds(graph: Graph, seeds: list[Seed], max_distance: int = 1) -> dict[int, Expansion]:
    """Expand seed symbols along high-value relations with distance decay."""
    expansions: dict[int, Expansion] = {}
    frontier = [(seed.symbol.id, 0, seed.symbol.id, "seed") for seed in seeds[:3]]
    visited = {seed.symbol.id for seed in seeds[:3]}
    while frontier:
        current, distance, origin, relation = frontier.pop(0)
        if distance >= max_distance:
            continue
        for edge in graph.out_edges(current, _HIGH_VALUE_RELATIONS):
            if edge.dst in visited:
                continue
            visited.add(edge.dst)
            expansions[edge.dst] = Expansion(
                symbol_id=edge.dst,
                distance=distance + 1,
                via=origin,
                relation=edge.kind,
                confidence=edge.confidence,
            )
            frontier.append((edge.dst, distance + 1, origin, edge.kind))
    return expansions


def exactness_bonus(symbol: Symbol, normalized_query: str) -> float:
    """Return a small bonus when the query names the symbol exactly."""
    if not normalized_query:
        return 0.0
    name = symbol.name.lower()
    qualified = symbol.qualified_name.lower()
    if name == normalized_query:
        return 0.35
    if qualified.endswith(f"::{normalized_query}") or qualified.endswith(f".{normalized_query}"):
        return 0.30
    if normalized_query in name:
        return 0.08
    return 0.0


def proximity_bonus(expansion: Expansion | None) -> float:
    """Return a capped graph-proximity bonus."""
    if expansion is None:
        return 0.0
    return max(0.02, 0.12 / expansion.distance)


def test_bonus(symbol: Symbol, wants_tests: bool) -> float:
    """Prefer tests only when the query asks for them."""
    is_test = symbol.name.lower().startswith("test") or "test" in symbol.file_path.lower()
    if wants_tests:
        return 0.12 if is_test else 0.0
    return -0.06 if is_test else 0.0


def co_location_bonus(symbol: Symbol, seed_files: set[str]) -> float:
    """Reward candidates in the same file as a seed (small and capped)."""
    return 0.04 if symbol.file_path in seed_files else 0.0


def is_hub(symbol: Symbol, degree: int, threshold: int) -> bool:
    """Return whether a symbol is a structural hub that should not dominate overviews."""
    return symbol.kind in {SymbolKind.FILE, SymbolKind.MODULE} or degree >= threshold


def edge_label(edge: Edge, direction: str) -> str:
    """Return a human-readable relation label."""
    labels = {
        ("calls", "in"): "caller",
        ("calls", "out"): "callee",
        ("imports", "in"): "importer",
        ("imports", "out"): "imports",
        ("imports_from", "in"): "importer",
        ("imports_from", "out"): "imports_from",
        ("inherits", "in"): "inherited_by",
        ("inherits", "out"): "inherits",
        ("implements", "in"): "implemented_by",
        ("implements", "out"): "implements",
        ("tests", "in"): "tested_by",
        ("tests", "out"): "tests",
        ("references", "in"): "referenced_by",
        ("references", "out"): "references",
        ("contains", "in"): "contained_by",
        ("contains", "out"): "contains",
        ("defines", "in"): "defined_by",
        ("defines", "out"): "defines",
    }
    return labels.get((edge.kind, direction), edge.kind if direction == "out" else f"{edge.kind}_by")
