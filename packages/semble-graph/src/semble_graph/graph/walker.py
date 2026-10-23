"""Deterministic repository walker with gitignore and vendor exclusions."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pathspec

from semble_graph.config import Settings

LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "bower_components",
    "vendor",
    "dist",
    "build",
    "target",
    "out",
    "bin",
    "obj",
    ".next",
    ".nuxt",
    ".svelte-kit",
    "coverage",
    "htmlcov",
    ".idea",
    ".vscode",
)


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """A file selected for extraction."""

    path: str
    absolute_path: Path
    language: str
    size: int


@dataclass(slots=True)
class WalkResult:
    """Result of a repository walk."""

    files: list[DiscoveredFile] = field(default_factory=list)
    skipped_large: int = 0
    truncated: bool = False

    @property
    def languages(self) -> dict[str, int]:
        """Count files per language."""
        counts: dict[str, int] = {}
        for file in self.files:
            counts[file.language] = counts.get(file.language, 0) + 1
        return dict(sorted(counts.items()))


def validate_repo(repo: str | Path) -> Path:
    """Validate a repository path and return its resolved directory.

    :raises FileNotFoundError: when the path does not exist.
    :raises NotADirectoryError: when the path is not a directory.
    :raises ValueError: when the path cannot be resolved safely.
    """
    path = Path(repo).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Repository does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Repository is not a directory: {path}")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"Refusing to index filesystem root: {resolved}")
    return resolved


def language_for_path(path: str | Path) -> str | None:
    """Return the tree-sitter language for a file path, if supported."""
    return LANGUAGES.get(Path(path).suffix.lower())


def _is_excluded(relative: str, is_dir: bool) -> bool:
    """Apply cheap built-in exclusions before consulting .gitignore files."""
    parts = PurePosixPath(relative).parts
    for part in parts:
        if part in DEFAULT_EXCLUDES:
            return True
        if is_dir and part.endswith(".egg-info"):
            return True
    return False


def _load_gitignore_specs(root: Path) -> list[tuple[PurePosixPath, pathspec.PathSpec]]:
    """Load all .gitignore files under the repository."""
    specs: list[tuple[PurePosixPath, pathspec.PathSpec]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        relative_dir = PurePosixPath(current.relative_to(root).as_posix())
        # Do not descend into vendor/build trees just to read their .gitignore files.
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_EXCLUDES)
        if ".gitignore" not in filenames:
            continue
        try:
            lines = (current / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        specs.append((relative_dir, pathspec.GitIgnoreSpec.from_lines(lines)))
    return specs


def _ignored(relative: str, specs: list[tuple[PurePosixPath, pathspec.PathSpec]]) -> bool:
    """Return whether a repo-relative path is ignored by any .gitignore file."""
    ignored = False
    for base, spec in specs:
        if base == PurePosixPath("."):
            candidate = relative
        elif relative == str(base) or relative.startswith(f"{base}/"):
            candidate = relative[len(str(base)) + 1 :] if relative != str(base) else ""
        else:
            continue
        if not candidate:
            continue
        if spec.match_file(candidate):
            ignored = True
        elif spec.match_file(candidate + "/"):
            ignored = True
    return ignored


def _walk_files(root: Path, settings: Settings) -> WalkResult:
    """Walk a repository and select supported source files."""
    result = WalkResult()
    specs = _load_gitignore_specs(root)
    root_resolved = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        relative_dir = current.relative_to(root)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            rel = (relative_dir / dirname).as_posix()
            if _is_excluded(rel, is_dir=True):
                continue
            full = current / dirname
            if full.is_symlink():
                try:
                    if not full.resolve().is_relative_to(root_resolved):
                        continue
                except OSError:
                    continue
            if _ignored(rel, specs):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            absolute = current / filename
            relative = (relative_dir / filename).as_posix()
            if relative.startswith("./"):
                relative = relative[2:]
            if _is_excluded(relative, is_dir=False) or _ignored(relative, specs):
                continue
            language = language_for_path(filename)
            if language is None:
                continue
            if absolute.is_symlink():
                try:
                    if not absolute.resolve().is_relative_to(root_resolved):
                        continue
                except OSError:
                    continue
            try:
                size = absolute.stat().st_size
            except OSError:
                continue
            if size > settings.max_file_bytes:
                result.skipped_large += 1
                continue
            result.files.append(DiscoveredFile(relative, absolute, language, size))
            if len(result.files) >= settings.max_files:
                result.truncated = True
                return result
    return result


def walk_repo(repo: str | Path, settings: Settings | None = None, file_paths: list[str] | None = None) -> WalkResult:
    """Discover source files in a repository.

    :param repo: Repository root.
    :param settings: Runtime settings (uses environment defaults when omitted).
    :param file_paths: Optional explicit repo-relative file set, typically from
        ``SembleIndex.chunks``. Only supported languages are retained.
    """
    settings = settings or Settings.from_env()
    root = validate_repo(repo)
    if file_paths is None:
        return _walk_files(root, settings)

    result = WalkResult()
    seen: set[str] = set()
    for raw_path in file_paths:
        relative = raw_path.replace("\\", "/")
        while relative.startswith("./"):
            relative = relative[2:]
        if not relative or relative in seen:
            continue
        seen.add(relative)
        language = language_for_path(relative)
        if language is None:
            continue
        absolute = (root / relative).resolve()
        try:
            if not absolute.is_relative_to(root):
                continue
        except ValueError:
            continue
        try:
            size = absolute.stat().st_size
        except OSError:
            continue
        if size > settings.max_file_bytes:
            result.skipped_large += 1
            continue
        result.files.append(DiscoveredFile(relative, absolute, language, size))
        if len(result.files) >= settings.max_files:
            result.truncated = True
            break
    result.files.sort(key=lambda file: file.path)
    return result


def read_source(file: DiscoveredFile, settings: Settings) -> bytes | None:
    """Read a discovered file, returning ``None`` when it is too large or unreadable."""
    try:
        if file.absolute_path.stat().st_size > settings.max_file_bytes:
            return None
        return file.absolute_path.read_bytes()
    except OSError:
        return None
