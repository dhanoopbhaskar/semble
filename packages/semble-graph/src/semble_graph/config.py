"""Configuration for the sidecar. All values can be overridden with environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "1"


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to a default."""
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable, falling back to a default."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def default_cache_dir() -> Path:
    """Return the sidecar cache root, honoring ``SEMBLE_GRAPH_CACHE_DIR``."""
    override = os.environ.get("SEMBLE_GRAPH_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    return base / "semble-graph"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for builds, queries, and the MCP server."""

    cache_dir: Path
    max_file_bytes: int
    max_files: int
    large_repo_threshold: int
    default_depth: int
    max_depth: int
    default_top_k: int
    overview_top_n: int
    stale_background_threshold: int
    revalidate_seconds: float
    revalidate_factor: float
    test_path_markers: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the current environment."""
        return cls(
            cache_dir=default_cache_dir(),
            max_file_bytes=_env_int("SEMBLE_MAX_FILE_BYTES", 1_000_000),
            max_files=_env_int("SEMBLE_GRAPH_MAX_FILES", 100_000),
            large_repo_threshold=_env_int("SEMBLE_GRAPH_LARGE_REPO_FILES", 5_000),
            default_depth=2,
            max_depth=_env_int("SEMBLE_GRAPH_MAX_DEPTH", 8),
            default_top_k=_env_int("SEMBLE_GRAPH_TOP_K", 10),
            overview_top_n=_env_int("SEMBLE_GRAPH_OVERVIEW_TOP_N", 15),
            stale_background_threshold=_env_int("SEMBLE_GRAPH_STALE_BACKGROUND_FILES", 500),
            revalidate_seconds=_env_float("SEMBLE_GRAPH_REVALIDATE_SECONDS", 2.0),
            revalidate_factor=_env_float("SEMBLE_GRAPH_REVALIDATE_FACTOR", 3.0),
            test_path_markers=("tests/", "test/", "__tests__/", "spec/"),
        )


def get_settings() -> Settings:
    """Return settings for the current environment."""
    return Settings.from_env()
