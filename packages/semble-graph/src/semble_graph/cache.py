"""Sidecar cache layout, locking, and atomic persistence."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from semble_graph.config import EXTRACTOR_VERSION, SCHEMA_VERSION, Settings, default_cache_dir


def _hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class CacheLayout:
    """Filesystem layout for one repository's graph cache."""

    root: Path
    key_dir: Path

    @property
    def graph_dir(self) -> Path:
        """Directory containing the current graph."""
        return self.key_dir / "graph"

    @property
    def raw_facts_path(self) -> Path:
        """Path to the incremental raw-facts cache."""
        return self.key_dir / "raw_facts.json"

    @property
    def lock_path(self) -> Path:
        """Path to the per-repository build lock."""
        return self.key_dir / "build.lock"

    @property
    def meta_path(self) -> Path:
        """Path to the current graph metadata."""
        return self.graph_dir / "graph.meta.json"

    def ensure(self) -> None:
        """Create the cache directories."""
        self.key_dir.mkdir(parents=True, exist_ok=True)

    def new_build_dir(self) -> Path:
        """Create a fresh temporary build directory."""
        return Path(tempfile.mkdtemp(prefix="build-", dir=self.key_dir))


def get_cache_layout(repo: str | Path, settings: Settings | None = None, ref: str | None = None) -> CacheLayout:
    """Return the cache layout for a repository."""
    settings = settings or Settings.from_env()
    root = Path(settings.cache_dir).expanduser()
    raw_repo = str(repo)
    identity = raw_repo if raw_repo.startswith(("http://", "https://")) else str(Path(repo).expanduser().resolve())
    key = _hash(identity, ref or "", str(SCHEMA_VERSION), EXTRACTOR_VERSION)
    return CacheLayout(root=root, key_dir=root / key)


def _guard_semble_cache(cache_root: Path) -> None:
    """Refuse to write into Semble's own cache tree."""
    semble_cache = os.environ.get("SEMBLE_CACHE_LOCATION")
    if not semble_cache:
        return
    try:
        cache_root.resolve().relative_to(Path(semble_cache).expanduser().resolve())
    except ValueError:
        return
    raise ValueError(f"SEMBLE_GRAPH_CACHE_DIR must not be inside Semble's cache ({semble_cache})")


@contextlib.contextmanager
def build_lock(layout: CacheLayout) -> Iterator[None]:
    """Acquire an exclusive per-repository build lock."""
    layout.ensure()
    _guard_semble_cache(layout.root)
    handle = layout.lock_path.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def atomic_install(build_dir: Path, target: Path) -> None:
    """Atomically replace ``target`` with a completed build directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    old: Path | None = None
    if target.exists():
        old = target.with_name(f"{target.name}.old-{uuid.uuid4().hex[:8]}")
        os.replace(target, old)
    try:
        os.replace(build_dir, target)
    except OSError:
        if old is not None and not target.exists():
            os.replace(old, target)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def read_json(path: Path) -> Any:
    """Read a JSON file, returning ``None`` when absent or corrupt."""
    try:
        return orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return None


def write_json(path: Path, value: Any) -> None:
    """Write deterministic JSON atomically."""
    payload = orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2)
    _atomic_write(path, payload)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes atomically."""
    _atomic_write(path, payload)


def cache_root(settings: Settings | None = None) -> Path:
    """Return the sidecar cache root."""
    return (settings or Settings.from_env()).cache_dir or default_cache_dir()


def remove_build_dir(path: Path) -> None:
    """Remove a temporary build directory."""
    shutil.rmtree(path, ignore_errors=True)
