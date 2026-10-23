from __future__ import annotations

from pathlib import Path

import pytest

from semble_graph import compat
from semble_graph.config import Settings
from semble_graph.graph.build import build_graph
from semble_graph.graph.model import SymbolKind
from semble_graph.graph.walker import language_for_path, walk_repo

pytestmark = pytest.mark.contract


def test_graph_and_semble_cover_the_same_files(sample_repo: Path, settings: Settings) -> None:
    index = compat.build_index(str(sample_repo))
    chunk_paths = {path for path in compat.chunk_files(index) if language_for_path(path)}
    walk_paths = {file.path for file in walk_repo(sample_repo, settings).files}
    assert chunk_paths == walk_paths

    graph = build_graph(sample_repo, settings, use_semble_files=True).graph
    graph_paths = {symbol.file_path for symbol in graph.symbols if symbol.kind is SymbolKind.FILE}
    assert graph_paths == chunk_paths
