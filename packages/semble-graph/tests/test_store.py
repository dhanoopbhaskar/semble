from __future__ import annotations

from pathlib import Path

from semble_graph.cache import get_cache_layout
from semble_graph.config import Settings
from semble_graph.graph.build import build_graph
from semble_graph.graph.freshness import repo_fingerprint
from semble_graph.graph.model import Graph
from semble_graph.graph.store import load_graph, read_meta, save_graph
from semble_graph.graph.walker import walk_repo


def test_store_roundtrip(tmp_path: Path, settings: Settings, sample_repo: Path) -> None:
    result = build_graph(sample_repo, settings)
    layout = get_cache_layout(sample_repo, settings)
    target = tmp_path / "graph"
    walk = walk_repo(sample_repo, settings)
    save_graph(
        result.graph,
        target,
        fingerprint=repo_fingerprint(sample_repo, walk),
        languages=walk.languages,
    )
    # Swap the completed target into the canonical location.
    from semble_graph.cache import atomic_install

    atomic_install(target, layout.graph_dir)
    meta = read_meta(layout)
    assert meta is not None
    assert meta["counts"]["symbols"] > 0
    loaded = load_graph(layout)
    assert loaded is not None
    assert len(loaded.symbols) == len(result.graph.symbols)
    assert len(loaded.edges) == len(result.graph.edges)
    assert loaded.file_symbol("src/payments/service.py") is not None


def test_corrupt_graph_returns_none(tmp_path: Path, settings: Settings, sample_repo: Path) -> None:
    build_graph(sample_repo, settings)
    layout = get_cache_layout(sample_repo, settings)
    (layout.graph_dir / "edges.npz").write_bytes(b"not-a-real-npz")
    assert load_graph(layout) is None


def test_graph_model_adds_file_symbols() -> None:
    graph = Graph(symbols=[], edges=[])
    assert graph.symbols == []
