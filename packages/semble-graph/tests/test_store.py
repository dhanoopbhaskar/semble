from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from semble_graph.cache import get_cache_layout
from semble_graph.config import Settings
from semble_graph.graph.build import build_graph
from semble_graph.graph.freshness import is_fresh, repo_fingerprint
from semble_graph.graph.model import Graph
from semble_graph.graph.query import GraphService, clear_cache
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


def test_build_attaches_persisted_fingerprint(sample_repo: Path, settings: Settings) -> None:
    result = build_graph(sample_repo, settings)
    walk = walk_repo(sample_repo, settings)
    assert result.graph.meta["fingerprint"] == repo_fingerprint(sample_repo, walk)
    assert is_fresh(result.graph.meta, repo_fingerprint(sample_repo, walk))


def test_process_cache_is_fresh_after_rebuild(sample_repo: Path, settings: Settings) -> None:
    clear_cache()
    first = GraphService(sample_repo, settings).load()
    assert first is not None
    second = GraphService(sample_repo, settings).load()
    assert second is not None
    assert second.status == "fresh"
    clear_cache()


def test_adaptive_revalidation_cooldown(sample_repo: Path, settings: Settings) -> None:
    service = GraphService(sample_repo, replace(settings, revalidate_seconds=2.0, revalidate_factor=3.0))
    assert service._cooldown_seconds(Graph(meta={"build_seconds": 10.0})) == 30.0
    assert service._cooldown_seconds(Graph(meta={"build_seconds": 0.1})) == 2.0
    assert service._cooldown_seconds(Graph(meta={})) == 2.0
    assert service._cooldown_seconds(Graph(meta={"build_seconds": 1.0})) == 3.0


def test_fingerprint_ignores_untracked_artifacts(sample_repo: Path, settings: Settings) -> None:
    before = repo_fingerprint(sample_repo, walk_repo(sample_repo, settings))
    (sample_repo / ".DS_Store").write_bytes(b"junk")
    (sample_repo / "notes.txt").write_text("not indexed", encoding="utf-8")
    after = repo_fingerprint(sample_repo, walk_repo(sample_repo, settings))
    assert after == before

    (sample_repo / "src/payments/extra.py").write_text("def extra() -> None:\n    pass\n", encoding="utf-8")
    changed = repo_fingerprint(sample_repo, walk_repo(sample_repo, settings))
    assert changed != before
