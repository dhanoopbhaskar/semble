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


def repo_fingerprint(repo: str | Path, walk: WalkResult | None = None) -> dict[str, object]:
    """Compute a deterministic fingerprint for a repository.

    Git repositories use ``HEAD``, ``git status --porcelain``, tracked file count
    and the maximum observed mtime. Non-git repositories hash the sorted
    ``(path, mtime_ns, size)`` tuples.
    """
    root = Path(repo).expanduser().resolve()
    max_mtime = 0
    if walk is not None:
        for file in walk.files:
            try:
                max_mtime = max(max_mtime, file.absolute_path.stat().st_mtime_ns)
            except OSError:
                continue

    if is_git_repo(root):
        head = (_git(root, "rev-parse", "HEAD") or "").strip()
        status = _git(root, "status", "--porcelain") or ""
        tracked = _git(root, "ls-files", "-z") or ""
        tracked_count = len([item for item in tracked.split("\0") if item])
        status_digest = hashlib.sha256(status.encode("utf-8")).hexdigest()
        return {
            "kind": "git",
            "head": head,
            "status_digest": status_digest,
            "tracked_count": tracked_count,
            "max_mtime_ns": max_mtime,
        }

    entries: list[tuple[str, int, int]] = []
    if walk is not None:
        for file in walk.files:
            try:
                stat = file.absolute_path.stat()
                entries.append((file.path, stat.st_mtime_ns, stat.st_size))
            except OSError:
                entries.append((file.path, 0, 0))
    encoded = json.dumps(sorted(entries), separators=(",", ":")).encode("utf-8")
    return {"kind": "files", "digest": hashlib.sha256(encoded).hexdigest(), "count": len(entries)}


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
