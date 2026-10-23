"""The only module allowed to import Semble.

Every upstream API adaptation lives here. If Semble changes its public surface,
this file and the contract tests are the only places that should need changes.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import threading
import warnings
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

SUPPORTED_SEMBLE = ">=0.7.0,<0.8.0"
_WARNED = threading.Lock()
_HAS_WARNED = False


@lru_cache(maxsize=1)
def semble_version() -> str | None:
    """Return the installed Semble version, or ``None`` when Semble is absent."""
    try:
        return importlib.metadata.version("semble")
    except importlib.metadata.PackageNotFoundError:
        return None


def semble_available() -> bool:
    """Return whether the Semble library is importable."""
    if semble_version() is None:
        return False
    try:
        return importlib.util.find_spec("semble") is not None
    except (ImportError, ValueError):
        return False


def _parse_version(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        digits = "".join(character for character in part if character.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])  # type: ignore[return-value]


def version_supported(version: str | None = None) -> bool:
    """Return whether the installed Semble version is inside the supported range."""
    version = version or semble_version()
    if version is None:
        return False
    return (0, 7, 0) <= _parse_version(version) < (0, 8, 0)


def _warn_once() -> None:
    global _HAS_WARNED
    with _WARNED:
        if _HAS_WARNED:
            return
        _HAS_WARNED = True
    version = semble_version()
    if version is not None and not version_supported(version):
        warnings.warn(
            f"semble {version} is outside the supported range {SUPPORTED_SEMBLE}; "
            "Semble-backed features will run optimistically and may fail open.",
            RuntimeWarning,
            stacklevel=3,
        )


CONTENT_NAMES = ("code", "docs", "config")


def content_names(content: Sequence[str] | None = None, include_text_files: bool = False) -> tuple[str, ...]:
    """Normalize content names the way Semble's CLI does."""
    if include_text_files or content is None or "all" in content:
        return CONTENT_NAMES
    selected = tuple(name for name in dict.fromkeys(content) if name in CONTENT_NAMES)
    return selected or ("code",)


def _content_types(content: tuple[str, ...]) -> tuple[Any, ...]:
    """Convert string content names to Semble's ContentType values."""
    from semble import ContentType

    values: list[Any] = []
    for item in content:
        try:
            values.append(ContentType(item))
        except ValueError:
            continue
    return tuple(values or [ContentType.CODE])


def is_git_url(path: str) -> bool:
    """Return whether a source is a remote git URL."""
    from semble.utils import is_git_url as _is_git_url

    return _is_git_url(path)


def build_index(repo: str, content: tuple[str, ...] = ("code",)) -> Any:
    """Build or load a Semble index for a local path or git URL."""
    _warn_once()
    from semble import SembleIndex

    types = _content_types(tuple(content))
    if is_git_url(repo):
        return SembleIndex.from_git(repo, content=types)
    return SembleIndex.from_path(repo, content=types)


def merge_indexes(parts: Sequence[tuple[str, Any]]) -> Any:
    """Merge several indexes into one, prefixing chunk paths with repo labels."""
    _warn_once()
    from semble import SembleIndex

    return SembleIndex.merge(parts)


def search(index: Any, query: str, top_k: int, max_snippet_lines: int | None = None) -> list[Any]:
    """Search a Semble index."""
    _warn_once()
    return list(index.search(query, top_k=top_k, max_snippet_lines=max_snippet_lines))


def resolve_chunk(chunks: list[Any], file_path: str, line: int) -> Any | None:
    """Return the Semble chunk containing a position, with Semble's boundary rules."""
    from semble.utils import resolve_chunk as _resolve_chunk

    return _resolve_chunk(chunks, file_path, line)


def format_results(
    query: str,
    results: list[Any],
    max_snippet_lines: int | None = None,
    repos: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Render Semble search results exactly like Semble does."""
    from semble.utils import format_results as _format_results

    return _format_results(query, results, max_snippet_lines, repos)


def chunk_files(index: Any) -> set[str]:
    """Return the file paths covered by a Semble index."""
    _warn_once()
    return {chunk.file_path for chunk in index.chunks}


def chunk_at(index: Any, file_path: str, line: int) -> Any | None:
    """Return the Semble chunk containing a position."""
    _warn_once()
    best = None
    best_size = None
    for chunk in index.chunks:
        if chunk.file_path != file_path:
            continue
        if chunk.start_line <= line <= chunk.end_line:
            size = chunk.end_line - chunk.start_line
            if best_size is None or size < best_size:
                best = chunk
                best_size = size
    return best


def find_related(index: Any, file_path: str, line: int, top_k: int, max_snippet_lines: int | None = None) -> list[Any]:
    """Run Semble's public ``find_related`` for a file/line."""
    _warn_once()
    chunk = resolve_chunk(index.chunks, file_path, line)
    if chunk is None:
        return []
    return list(index.find_related(chunk, top_k=top_k, max_snippet_lines=max_snippet_lines))


def status() -> dict[str, Any]:
    """Return compatibility diagnostics for ``semble-graph doctor``."""
    version = semble_version()
    return {
        "semble_available": semble_available(),
        "semble_version": version,
        "supported_range": SUPPORTED_SEMBLE,
        "supported": version_supported(version),
    }


def fallback_message(reason: str) -> str:
    """Return a structured message telling the agent to use Semble search instead."""
    return (
        "Graph-aware search is unavailable for this request "
        f"({reason}). Use Semble's `search` tool for text retrieval, then `graph_symbol`, "
        "`graph_neighbors`, or `graph_impact` for graph relationships."
    )


def agent_ids() -> list[str]:
    """Return the agent ids accepted by Semble's installer."""
    try:
        from semble.installer.agents import AGENTS
    except Exception:
        return []
    return [agent.id for agent in AGENTS]


def integration_types() -> list[str]:
    """Return the integration ids accepted by Semble's installer."""
    try:
        from semble.installer.agents import IntegrationType
    except Exception:
        return []
    return [item.value for item in IntegrationType]


def run_installer(
    command: str,
    agent_ids: list[str] | None = None,
    integration_ids: list[str] | None = None,
    yes: bool = False,
) -> None:
    """Delegate ``install`` / ``uninstall`` to Semble's installer."""
    _warn_once()
    from semble.installer import run
    from semble.installer.agents import IntegrationType

    selected = None if integration_ids is None else [IntegrationType(item) for item in integration_ids]
    run(command, agent_ids=agent_ids, integration_ids=selected, yes=yes)  # type: ignore[arg-type]


def clear_cache(clear_type: str) -> None:
    """Delegate ``clear`` to Semble's cache-clearing routine."""
    _warn_once()
    from semble.cli import _run_clear

    _run_clear(clear_type)  # type: ignore[arg-type]


def file_types(content: Sequence[str]) -> dict[str, list[str]]:
    """Return the file extensions Semble indexes per content type."""
    _warn_once()
    from semble.index.files import get_extensions

    types = _content_types(tuple(content))
    return {content_type.value: list(get_extensions([content_type])) for content_type in types}


def savings_report() -> str:
    """Return Semble's token-savings report."""
    _warn_once()
    from semble.stats import format_savings_report

    return format_savings_report()
