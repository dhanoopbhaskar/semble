"""Repository fingerprints and staleness policy."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from semble_graph.config import Settings
from semble_graph.graph.walker import WalkResult


def _git(repo: Path, *args: str) -> str | None:
    """Run a read-only git command, returning ``None`` when unavailable."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def is_git_repo(repo: Path) -> bool:
    """Return whether the directory is inside a git work tree."""
    result = _git(repo, "rev-parse", "--is-inside-work-tree")
    return result is not None and result.strip() == "true"


def _file_entries(walk: WalkResult | None) -> list[tuple[str, int, int]]:
    """Return sorted ``(path, mtime_ns, size)`` tuples for the indexed file set."""
    entries: list[tuple[str, int, int]] = []
    if walk is None:
        return entries
    for file in walk.files:
        try:
            stat = file.absolute_path.stat()
            entries.append((file.path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            entries.append((file.path, 0, 0))
    entries.sort()
    return entries


def repo_fingerprint(repo: str | Path, walk: WalkResult | None = None) -> dict[str, object]:
    """Compute a deterministic fingerprint for a repository.

    The fingerprint covers only the files the graph actually indexes: their
    repo-relative paths, modification times, and sizes. Untracked artifacts such
    as ``.DS_Store`` and editor caches therefore never invalidate the graph.

    Git repositories additionally include ``HEAD`` as a cheap branch/commit
    signal. The previous implementation hashed ``git status --porcelain`` for the
    whole work tree, which made every untracked file trigger a full rebuild.
    """
    root = Path(repo).expanduser().resolve()
    entries = _file_entries(walk)
    encoded = json.dumps(entries, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()

    if is_git_repo(root):
        head = (_git(root, "rev-parse", "HEAD") or "").strip()
        return {
            "kind": "git",
            "head": head,
            "files_digest": digest,
            "files_count": len(entries),
        }

    return {"kind": "files", "digest": digest, "count": len(entries)}


def fingerprint_digest(fingerprint: dict[str, object]) -> str:
    """Hash a fingerprint dict into a short stable digest."""
    payload = json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_fresh(meta: dict[str, object] | None, fingerprint: dict[str, object]) -> bool:
    """Return whether stored metadata matches the current fingerprint."""
    if not meta:
        return False
    stored = meta.get("fingerprint")
    if not isinstance(stored, dict):
        return False
    return fingerprint_digest(stored) == fingerprint_digest(fingerprint)


def should_rebuild_in_background(file_count: int, settings: Settings | None = None) -> bool:
    """Return whether a stale graph should be served while rebuilding."""
    settings = settings or Settings.from_env()
    threshold = settings.stale_background_threshold
    return threshold > 0 and file_count >= threshold
