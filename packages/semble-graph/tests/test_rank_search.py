from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from semble_graph.config import Settings
from semble_graph.graph.build import build_graph
from semble_graph.graph.query import resolve_symbols
from semble_graph.graph.rank import (
    Seed,
    exactness_bonus,
    expand_seeds,
    normalize_scores,
    proximity_bonus,
)
from semble_graph.graph.rank import (
    test_bonus as rank_test_bonus,
)
from semble_graph.search import clear_index_cache, graph_search


def test_rank_helpers() -> None:
    assert normalize_scores([2.0, 1.0]) == [1.0, 0.5]
    assert normalize_scores([]) == []
    assert normalize_scores([0.0, 0.0]) == [1.0, 0.5]


def test_exactness_and_test_bonuses(sample_repo: Path, settings: Settings) -> None:
    graph = build_graph(sample_repo, settings).graph
    service = resolve_symbols(graph, "PaymentService")[0]
    assert exactness_bonus(service, "paymentservice") == 0.35
    assert exactness_bonus(service, "Payment") < 0.35
    assert rank_test_bonus(service, wants_tests=False) == 0.0
    test_symbol = next(symbol for symbol in graph.symbols if symbol.name == "test_charge")
    assert rank_test_bonus(test_symbol, wants_tests=True) > rank_test_bonus(test_symbol, wants_tests=False)


def test_seed_expansion(sample_repo: Path, settings: Settings) -> None:
    graph = build_graph(sample_repo, settings).graph
    charge = resolve_symbols(graph, "PaymentService.charge")[0]
    expansion = expand_seeds(graph, [Seed(symbol=charge, base_score=1.0, rank=0)], max_distance=2)
    assert expansion
    assert all(proximity_bonus(item) > 0 for item in expansion.values())
    assert max(item.distance for item in expansion.values()) <= 2


@dataclass
class FakeChunk:
    content: str
    file_path: str
    start_line: int
    end_line: int
    language: str = "python"


@dataclass
class FakeResult:
    chunk: FakeChunk
    score: float


class FakeIndex:
    pass


def test_graph_search_intent_and_exactness(sample_repo: Path, settings: Settings, monkeypatch) -> None:
    build_graph(sample_repo, settings)
    clear_index_cache()
    fake = FakeResult(
        chunk=FakeChunk(
            content="class PaymentService:\n    pass\n",
            file_path="src/payments/service.py",
            start_line=5,
            end_line=8,
        ),
        score=1.0,
    )

    from semble_graph import compat

    monkeypatch.setattr(compat, "semble_available", lambda: True)
    monkeypatch.setattr("semble_graph.search._get_index", lambda repo, content=("code",): FakeIndex())
    monkeypatch.setattr(compat, "search", lambda index, query, top_k: [fake])

    result = graph_search(sample_repo, "PaymentService", top_k=5, settings=settings)
    assert result["graph_status"] in {"fresh", "rebuilt"}
    assert result["intent"] in {"symbol", "conceptual"}
    assert result["results"]
    assert result["results"][0]["qualified_name"].endswith("PaymentService")


def test_graph_search_fails_open_without_graph(sample_repo: Path, settings: Settings, monkeypatch) -> None:
    clear_index_cache()
    fake = FakeResult(
        chunk=FakeChunk(content="x = 1\n", file_path="src/payments/service.py", start_line=1, end_line=1),
        score=2.0,
    )
    from semble_graph import compat
    from semble_graph.graph.query import GraphService

    monkeypatch.setattr(GraphService, "load", lambda self, rebuild=True, background=True: None)
    monkeypatch.setattr(compat, "semble_available", lambda: True)
    monkeypatch.setattr("semble_graph.search._get_index", lambda repo, content=("code",): FakeIndex())
    monkeypatch.setattr(compat, "search", lambda index, query, top_k: [fake])

    result = graph_search(sample_repo, "anything", top_k=3, settings=settings)
    assert result["graph_status"] == "unavailable"
    assert result["results"][0]["file_path"] == "src/payments/service.py"
