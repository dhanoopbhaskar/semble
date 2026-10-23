"""Shared extraction data structures and tree-sitter helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from tree_sitter import Node

from semble_graph.graph.model import SymbolKind

_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*$")
_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)(test_[^/]+|[^/]+_test\.[^.]+|[^/]+\.(spec|test)\.[^.]+)$")


@dataclass(slots=True)
class RawSymbol:
    """A definition found in one file, before global resolution."""

    name: str
    qualified_name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    parent: str | None = None


@dataclass(slots=True)
class RawImport:
    """An import statement found in one file."""

    module: str
    line: int
    names: list[tuple[str, str]] = field(default_factory=list)
    alias: str | None = None
    is_from: bool = False
    level: int = 0


@dataclass(slots=True)
class RawCall:
    """A call site (or constructor call) found in one file."""

    expression: str
    simple_name: str
    receiver: str | None
    enclosing: str | None
    line: int


@dataclass(slots=True)
class RawInherit:
    """An inheritance or implements clause."""

    owner: str
    base: str
    base_full: str
    relation: str
    line: int


@dataclass(slots=True)
class RawReference:
    """A non-call identifier reference (e.g. a decorator or annotation)."""

    expression: str
    simple_name: str
    receiver: str | None
    enclosing: str | None
    line: int


@dataclass(slots=True)
class FileFacts:
    """Raw, file-local facts. No cross-file resolution happens here."""

    path: str
    language: str
    symbols: list[RawSymbol] = field(default_factory=list)
    imports: list[RawImport] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    inherits: list[RawInherit] = field(default_factory=list)
    references: list[RawReference] = field(default_factory=list)

    def add_symbol(self, symbol: RawSymbol) -> None:
        """Add a symbol, merging duplicate qualified names (e.g. overloads)."""
        for existing in self.symbols:
            if existing.qualified_name == symbol.qualified_name:
                existing.end_line = max(existing.end_line, symbol.end_line)
                existing.start_line = min(existing.start_line, symbol.start_line)
                return
        self.symbols.append(symbol)

    def qualified(self, name: str) -> str:
        """Return a file-qualified name for a top-level definition."""
        return f"{self.path}::{name}"


def text(node: Node | None, source: bytes) -> str:
    """Return the UTF-8 text of a node."""
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def line_of(node: Node) -> int:
    """Return the 1-indexed start line of a node."""
    return node.start_point[0] + 1


def end_line_of(node: Node) -> int:
    """Return the 1-indexed end line of a node."""
    return max(node.start_point[0] + 1, node.end_point[0] + 1)


def child_text(node: Node | None, field: str, source: bytes) -> str:
    """Return the text of a named child field."""
    if node is None:
        return ""
    return text(node.child_by_field_name(field), source)


def clean_type_name(expression: str) -> str:
    """Reduce a type/call expression to its terminal identifier."""
    value = expression.strip()
    value = value.split("<", 1)[0]
    value = value.replace("[]", "").replace("?", "").replace("*", "").replace("&", "")
    value = value.strip()
    if "::" in value:
        value = value.rsplit("::", 1)[-1]
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    if "(" in value:
        value = value.split("(", 1)[0]
    value = value.strip()
    return value if _IDENTIFIER.match(value) else value


def split_expression(expression: str) -> tuple[str | None, str]:
    """Split ``a.b.c`` into ``("a.b", "c")``."""
    value = expression.strip()
    if not value:
        return None, ""
    if "::" in value:
        receiver, name = value.rsplit("::", 1)
        return receiver, name
    if "." in value:
        receiver, name = value.rsplit(".", 1)
        return receiver, name
    return None, value


def scoped_name(scope: list[str], name: str) -> str:
    """Build a dotted qualified name from a scope stack."""
    return ".".join([*scope, name]) if scope else name


def is_test_path(path: str) -> bool:
    """Return whether a path looks like a test file."""
    normalized = PurePosixPath(path).as_posix()
    return bool(_TEST_FILE.search(normalized))


def is_test_name(name: str) -> bool:
    """Return whether a symbol name looks like a test."""
    lowered = name.lower()
    return lowered.startswith("test") or lowered.startswith("spec") or lowered in {"setup", "teardown"}


def enclosing_symbol(facts: FileFacts, scope: list[RawSymbol]) -> str | None:
    """Return the qualified name of the innermost enclosing definition."""
    return scope[-1].qualified_name if scope else None
