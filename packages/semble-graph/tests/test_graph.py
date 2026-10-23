from __future__ import annotations

from pathlib import Path

from semble_graph.config import Settings
from semble_graph.graph.build import build_graph
from semble_graph.graph.model import SymbolKind
from semble_graph.graph.query import (
    graph_impact,
    graph_neighbors,
    graph_overview,
    graph_path,
    graph_symbol,
    resolve_symbols,
)
from semble_graph.graph.walker import walk_repo


def test_walker_respects_gitignore_and_vendor_excludes(sample_repo: Path, settings: Settings) -> None:
    result = walk_repo(sample_repo, settings)
    paths = {file.path for file in result.files}
    assert "src/payments/service.py" in paths
    assert "ignored.py" not in paths
    assert "vendor/vendored.py" not in paths
    discovered = [file.path for file in result.files]
    assert discovered == sorted(discovered)


def test_build_creates_symbols_and_imports(sample_repo: Path, settings: Settings) -> None:
    result = build_graph(sample_repo, settings)
    graph = result.graph
    assert result.stats.files >= 10
    assert result.stats.symbols > 20

    service = graph_symbol(graph, "PaymentService")
    assert service["count"] == 1
    symbol = service["matches"][0]
    assert symbol["kind"] == "class"
    assert symbol["file_path"] == "src/payments/service.py"

    charge = resolve_symbols(graph, "PaymentService.charge")[0]
    callers = {edge.src for edge in graph.in_edges(charge.id, {"calls"})}
    caller_names = {graph.require_symbol(item).name for item in callers}
    assert "retry" in caller_names

    service_file = graph.file_symbol("src/payments/service.py")
    importers = {
        graph.require_symbol(edge.src).file_path
        for edge in graph.in_edges(service_file.id, {"imports", "imports_from"})
    }
    assert "tests/test_service.py" in importers
    assert "src/payments/__init__.py" in importers


def test_cross_language_resolution(sample_repo: Path, settings: Settings) -> None:
    graph = build_graph(sample_repo, settings).graph

    helper = resolve_symbols(graph, "helper")
    helper_ts = next(symbol for symbol in helper if symbol.file_path == "web/helper.ts")
    callers = {graph.require_symbol(edge.src).name for edge in graph.in_edges(helper_ts.id, {"calls"})}
    assert {"run", "retry"} <= callers

    go_method = resolve_symbols(graph, "Store.Save")[0]
    assert go_method.kind is SymbolKind.METHOD

    rust_run = resolve_symbols(graph, "Engine.run")[0]
    rust_helper = next(symbol for symbol in resolve_symbols(graph, "helper") if symbol.file_path == "rs/util.rs")
    assert any(edge.dst == rust_helper.id for edge in graph.out_edges(rust_run.id, {"calls"}))

    java_run = next(symbol for symbol in resolve_symbols(graph, "run") if symbol.file_path.endswith("Service.java"))
    java_help = next(symbol for symbol in resolve_symbols(graph, "help") if symbol.file_path.endswith("Helper.java"))
    assert any(edge.dst == java_help.id for edge in graph.out_edges(java_run.id, {"calls"}))


def test_test_edges_and_impact(sample_repo: Path, settings: Settings) -> None:
    graph = build_graph(sample_repo, settings).graph
    charge = resolve_symbols(graph, "PaymentService.charge")[0]
    tests = graph.in_edges(charge.id, {"tests"})
    assert tests
    assert any("test_service.py" in graph.require_symbol(edge.src).file_path for edge in tests)

    impact = graph_impact(graph, paths=["src/payments/gateway.py"], depth=2)
    assert impact["total"] > 0
    names = {item["symbol"]["name"] for group in impact["results"] for item in group["symbols"] if item["symbol"]}
    assert "charge" in names or "PaymentService" in names


def test_neighbors_and_path(sample_repo: Path, settings: Settings) -> None:
    graph = build_graph(sample_repo, settings).graph
    neighbors = graph_neighbors(graph, symbol="PaymentService.charge", relation="callers")
    names = {item["symbol"]["qualified_name"] for item in neighbors["neighbors"]}
    assert any(name.endswith("PaymentService.retry") for name in names)

    path = graph_path(graph, "tests/test_service.py::test_charge", "PaymentService.charge")
    assert path["found"] is True
    assert path["hops"] >= 1


def test_overview(sample_repo: Path, settings: Settings) -> None:
    graph = build_graph(sample_repo, settings).graph
    overview = graph_overview(graph, top_n=5)
    assert overview["core_symbols"]
    assert len(overview["core_symbols"]) <= 5
    assert overview["entry_points"]


def test_incremental_build_reuses_unchanged_files(sample_repo: Path, settings: Settings) -> None:
    first = build_graph(sample_repo, settings)
    second = build_graph(sample_repo, settings)
    assert second.stats.parsed == 0
    assert second.stats.reused == second.stats.files
    assert second.stats.symbols == first.stats.symbols

    changed = sample_repo / "src/payments/models.py"
    changed.write_text(changed.read_text() + "\n\ndef extra() -> int:\n    return 1\n", encoding="utf-8")
    third = build_graph(sample_repo, settings)
    assert third.stats.parsed == 1
    assert third.stats.reused == third.stats.files - 1
    assert resolve_symbols(third.graph, "extra")
