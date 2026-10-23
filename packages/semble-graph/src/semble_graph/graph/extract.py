"""Language dispatch for tree-sitter extraction."""

from __future__ import annotations

import logging
from pathlib import Path

from semble_grammars import LanguageNotFoundError, UnsupportedPlatformError, get_parser

from semble_graph.graph.extractors import FileFacts
from semble_graph.graph.extractors import go as go_extractor
from semble_graph.graph.extractors import java as java_extractor
from semble_graph.graph.extractors import python as python_extractor
from semble_graph.graph.extractors import rust as rust_extractor
from semble_graph.graph.extractors import typescript as typescript_extractor
from semble_graph.graph.walker import DiscoveredFile, Settings, read_source

logger = logging.getLogger(__name__)

_GRAMMAR_NAMES = {
    "typescript": "typescript",
    "tsx": "tsx",
    "javascript": "javascript",
    "python": "python",
    "go": "go",
    "rust": "rust",
    "java": "java",
}

_PARSERS = {
    "python": python_extractor.parse,
    "typescript": typescript_extractor.parse,
    "tsx": typescript_extractor.parse,
    "javascript": typescript_extractor.parse,
    "go": go_extractor.parse,
    "rust": rust_extractor.parse,
    "java": java_extractor.parse,
}


def extract_file(file: DiscoveredFile, settings: Settings) -> FileFacts:
    """Extract raw facts from a file, failing open when a grammar is unavailable."""
    source = read_source(file, settings)
    if source is None:
        return FileFacts(path=file.path, language=file.language)
    grammar = _GRAMMAR_NAMES.get(file.language)
    parser_fn = _PARSERS.get(file.language)
    if grammar is None or parser_fn is None:
        return FileFacts(path=file.path, language=file.language)
    try:
        parser = get_parser(grammar)
        tree = parser.parse(source)
    except (LanguageNotFoundError, UnsupportedPlatformError, OSError) as exc:
        logger.warning("Grammar %s unavailable for %s: %s", grammar, file.path, exc)
        return FileFacts(path=file.path, language=file.language)
    except Exception:
        logger.exception("Failed to parse %s", file.path)
        return FileFacts(path=file.path, language=file.language)
    try:
        return parser_fn(file.path, source, tree.root_node)
    except Exception:
        logger.exception("Extractor failed for %s", file.path)
        return FileFacts(path=file.path, language=file.language)


def supported_suffixes() -> tuple[str, ...]:
    """Return supported source suffixes."""
    from semble_graph.graph.walker import LANGUAGES

    return tuple(sorted(LANGUAGES))


def extract_path(path: Path, source: bytes, language: str) -> FileFacts:
    """Extract facts from an explicit source blob (used by tests and fixtures)."""
    parser_fn = _PARSERS.get(language)
    grammar = _GRAMMAR_NAMES.get(language)
    if parser_fn is None or grammar is None:
        return FileFacts(path=path.as_posix(), language=language)
    try:
        tree = get_parser(grammar).parse(source)
    except Exception:
        return FileFacts(path=path.as_posix(), language=language)
    return parser_fn(path.as_posix(), source, tree.root_node)
