"""Build pipeline: walk, extract, resolve, persist."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from semble_graph import compat
from semble_graph.cache import CacheLayout, atomic_install, build_lock, get_cache_layout, read_json, write_json
from semble_graph.config import EXTRACTOR_VERSION, SCHEMA_VERSION, Settings
from semble_graph.graph.extract import extract_file
from semble_graph.graph.extractors.base import (
    FileFacts,
    RawCall,
    RawImport,
    RawInherit,
    RawReference,
    RawSymbol,
)
from semble_graph.graph.freshness import repo_fingerprint
from semble_graph.graph.model import Graph, SymbolKind
from semble_graph.graph.resolve import resolve
from semble_graph.graph.store import read_meta, save_graph
from semble_graph.graph.walker import WalkResult, walk_repo

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BuildStats:
    """Statistics for one build."""

    files: int = 0
    parsed: int = 0
    reused: int = 0
    skipped_large: int = 0
    symbols: int = 0
    edges: int = 0
    seconds: float = 0.0
    truncated: bool = False
    languages: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class BuildResult:
    """A graph plus its cache layout and build statistics."""

    graph: Graph
    layout: CacheLayout
    stats: BuildStats


def _symbol_to_dict(symbol: RawSymbol) -> dict[str, Any]:
    return {
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind.value,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "parent": symbol.parent,
    }


def _facts_to_dict(facts: FileFacts) -> dict[str, Any]:
    return {
        "path": facts.path,
        "language": facts.language,
        "symbols": [_symbol_to_dict(symbol) for symbol in facts.symbols],
        "imports": [
            {
                "module": item.module,
                "line": item.line,
                "names": item.names,
                "alias": item.alias,
                "is_from": item.is_from,
                "level": item.level,
            }
            for item in facts.imports
        ],
        "calls": [
            {
                "expression": item.expression,
                "simple_name": item.simple_name,
                "receiver": item.receiver,
                "enclosing": item.enclosing,
                "line": item.line,
            }
            for item in facts.calls
        ],
        "inherits": [
            {
                "owner": item.owner,
                "base": item.base,
                "base_full": item.base_full,
                "relation": item.relation,
                "line": item.line,
            }
            for item in facts.inherits
        ],
        "references": [
            {
                "expression": item.expression,
                "simple_name": item.simple_name,
                "receiver": item.receiver,
                "enclosing": item.enclosing,
                "line": item.line,
            }
            for item in facts.references
        ],
    }


def _facts_from_dict(data: dict[str, Any]) -> FileFacts:
    return FileFacts(
        path=str(data["path"]),
        language=str(data["language"]),
        symbols=[
            RawSymbol(
                name=str(item["name"]),
                qualified_name=str(item["qualified_name"]),
                kind=SymbolKind(item["kind"]),
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
                parent=item.get("parent"),
            )
            for item in data.get("symbols", [])
        ],
        imports=[
            RawImport(
                module=str(item["module"]),
                line=int(item["line"]),
                names=[(str(name), str(alias)) for name, alias in item.get("names", [])],
                alias=item.get("alias"),
                is_from=bool(item.get("is_from", False)),
                level=int(item.get("level", 0)),
            )
            for item in data.get("imports", [])
        ],
        calls=[
            RawCall(
                expression=str(item["expression"]),
                simple_name=str(item["simple_name"]),
                receiver=item.get("receiver"),
                enclosing=item.get("enclosing"),
                line=int(item["line"]),
            )
            for item in data.get("calls", [])
        ],
        inherits=[
            RawInherit(
                owner=str(item["owner"]),
                base=str(item["base"]),
                base_full=str(item["base_full"]),
                relation=str(item["relation"]),
                line=int(item["line"]),
            )
            for item in data.get("inherits", [])
        ],
        references=[
            RawReference(
                expression=str(item["expression"]),
                simple_name=str(item["simple_name"]),
                receiver=item.get("receiver"),
                enclosing=item.get("enclosing"),
                line=int(item["line"]),
            )
            for item in data.get("references", [])
        ],
    )


def _load_raw_cache(layout: CacheLayout) -> dict[str, dict[str, Any]]:
    data = read_json(layout.raw_facts_path)
    if not isinstance(data, dict):
        return {}
    if data.get("schema_version") != SCHEMA_VERSION or data.get("extractor_version") != EXTRACTOR_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_raw_cache(layout: CacheLayout, entries: dict[str, dict[str, Any]]) -> None:
    write_json(
        layout.raw_facts_path,
        {
            "schema_version": SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "files": entries,
        },
    )


def _stat_key(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _discover(repo: Path, settings: Settings, use_semble_files: bool) -> WalkResult:
    if use_semble_files and compat.semble_available():
        try:
            paths = compat.chunk_files(str(repo))
            if paths:
                return walk_repo(repo, settings, file_paths=sorted(paths))
        except Exception:
            logger.warning("Falling back to the sidecar walker after Semble file discovery failed", exc_info=True)
    return walk_repo(repo, settings)


def build_graph(
    repo: str | Path,
    settings: Settings | None = None,
    *,
    force: bool = False,
    use_semble_files: bool = False,
) -> BuildResult:
    """Build (or incrementally rebuild) a repository graph and install it atomically."""
    settings = settings or Settings.from_env()
    started = time.perf_counter()
    root = Path(repo).expanduser().resolve()
    layout = get_cache_layout(root, settings)
    layout.ensure()

    with build_lock(layout):
        walk = _discover(root, settings, use_semble_files)
        fingerprint = repo_fingerprint(root, walk)
        cached = {} if force else _load_raw_cache(layout)
        facts: list[FileFacts] = []
        entries: dict[str, dict[str, Any]] = {}
        parsed = 0
        reused = 0

        for file in walk.files:
            try:
                mtime_ns, size = _stat_key(file.absolute_path)
            except OSError:
                continue
            cache_entry = cached.get(file.path)
            if (
                not force
                and isinstance(cache_entry, dict)
                and cache_entry.get("mtime_ns") == mtime_ns
                and cache_entry.get("size") == size
                and cache_entry.get("language") == file.language
            ):
                try:
                    file_facts = _facts_from_dict(cache_entry["facts"])
                    facts.append(file_facts)
                    entries[file.path] = cache_entry
                    reused += 1
                    continue
                except (KeyError, TypeError, ValueError):
                    pass
            file_facts = extract_file(file, settings)
            facts.append(file_facts)
            entries[file.path] = {
                "mtime_ns": mtime_ns,
                "size": size,
                "language": file.language,
                "facts": _facts_to_dict(file_facts),
            }
            parsed += 1

        _save_raw_cache(layout, entries)
        graph = resolve(facts)
        stats = BuildStats(
            files=len(walk.files),
            parsed=parsed,
            reused=reused,
            skipped_large=walk.skipped_large,
            symbols=len(graph.symbols),
            edges=len(graph.edges),
            seconds=time.perf_counter() - started,
            truncated=walk.truncated,
            languages=walk.languages,
        )
        build_dir = layout.new_build_dir()
        try:
            save_graph(
                graph,
                build_dir,
                fingerprint=fingerprint,
                languages=walk.languages,
                semble_version=compat.semble_version(),
                build_seconds=stats.seconds,
            )
            atomic_install(build_dir, layout.graph_dir)
            # ``save_graph`` writes the fingerprint to disk; attach the persisted
            # metadata so an in-process cache entry is not considered stale forever.
            graph.meta = read_meta(layout) or {"fingerprint": fingerprint}
        except Exception:
            from semble_graph.cache import remove_build_dir

            remove_build_dir(build_dir)
            raise
    logger.info(
        "Built graph for %s: %d files (%d parsed, %d reused), %d symbols, %d edges in %.2fs",
        root,
        stats.files,
        stats.parsed,
        stats.reused,
        stats.symbols,
        stats.edges,
        stats.seconds,
    )
    return BuildResult(graph=graph, layout=layout, stats=stats)


def graph_status(repo: str | Path, settings: Settings | None = None) -> dict[str, Any]:
    """Return status information for a repository's graph."""
    settings = settings or Settings.from_env()
    root = Path(repo).expanduser().resolve()
    layout = get_cache_layout(root, settings)
    meta = read_meta(layout)
    if meta is None:
        return {
            "repo": str(root),
            "status": "missing",
            "cache_path": str(layout.graph_dir),
            "cache_key": layout.key_dir.name,
        }
    walk = walk_repo(root, settings)
    current = repo_fingerprint(root, walk)
    from semble_graph.graph.freshness import is_fresh

    return {
        "repo": str(root),
        "status": "fresh" if is_fresh(meta, current) else "stale",
        "cache_path": str(layout.graph_dir),
        "cache_key": layout.key_dir.name,
        "files": len(walk.files),
        "counts": meta.get("counts"),
        "languages": meta.get("languages"),
        "build_seconds": meta.get("build_seconds"),
        "built_at": meta.get("built_at"),
        "schema_version": meta.get("schema_version"),
        "semble_version": meta.get("semble_version"),
        "fingerprint": meta.get("fingerprint"),
    }
