from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from semble_graph.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_REPO = FIXTURES / "sample"

collect_ignore_glob = ["fixtures/*"]


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    """Return an isolated copy of the sample fixture repository."""
    destination = tmp_path / "sample"
    shutil.copytree(SAMPLE_REPO, destination)
    return destination


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Return settings with an isolated cache and synchronous rebuilds."""
    return replace(
        Settings.from_env(),
        cache_dir=tmp_path / "graph-cache",
        stale_background_threshold=0,
    )
