from __future__ import annotations

from pathlib import Path

import pytest

from semble_graph import compat

pytestmark = pytest.mark.contract


def test_semble_is_installed_for_contract() -> None:
    assert compat.semble_available(), "Install Semble (the `search` extra) to run contract tests."
    assert compat.version_supported(), f"Unsupported Semble version: {compat.semble_version()}"


def test_public_api_contract(sample_repo: Path) -> None:
    import semble

    assert set(semble.__all__) >= {
        "Chunk",
        "ContentType",
        "SearchResult",
        "SembleIndex",
        "__version__",
    }
    chunk_fields = {"content", "file_path", "start_line", "end_line", "language"}
    assert chunk_fields <= set(semble.Chunk.__dataclass_fields__)
    assert hasattr(semble.SembleIndex, "from_path")
    assert hasattr(semble.SembleIndex, "search")
    assert hasattr(semble.SembleIndex, "find_related")


def test_adapter_build_search_and_related(sample_repo: Path) -> None:
    index = compat.build_index(str(sample_repo))
    assert compat.chunk_files(index)
    results = compat.search(index, "payment gateway authorize", top_k=3)
    assert results
    first = results[0]
    assert hasattr(first, "chunk")
    assert hasattr(first, "score")
    related = compat.find_related(index, first.chunk.file_path, first.chunk.start_line, top_k=2)
    assert isinstance(related, list)


def test_version_guard_reports_range() -> None:
    status = compat.status()
    assert status["supported_range"] == compat.SUPPORTED_SEMBLE
    assert status["supported"] is True
