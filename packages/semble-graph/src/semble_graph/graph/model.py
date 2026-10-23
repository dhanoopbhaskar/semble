"""Core graph model: symbols, edges, and an in-memory graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator


class SymbolKind(str, Enum):
    """Kinds of code symbols represented in the graph."""

    FILE = "file"
    MODULE = "module"
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    TYPE = "type"
    CONSTANT = "constant"


class Confidence(str, Enum):
    """Confidence label for an edge."""

    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


EDGE_KINDS: tuple[str, ...] = (
    "defines",
    "contains",
    "imports",
    "imports_from",
    "calls",
    "references",
    "inherits",
    "implements",
    "tests",
)

CONFIDENCE_VALUES: tuple[str, ...] = tuple(c.value for c in Confidence)


@dataclass(frozen=True, slots=True)
class Symbol:
    """A definition in the repository."""

    id: int
    name: str
    qualified_name: str
    kind: SymbolKind
    file_path: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize the symbol to a JSON-compatible dict."""
        return {
            "id": self.id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind.value,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Symbol:
        """Deserialize a symbol."""
        return cls(
            id=int(data["id"]),
            name=str(data["name"]),
            qualified_name=str(data["qualified_name"]),
            kind=SymbolKind(data["kind"]),
            file_path=str(data["file_path"]),
            start_line=int(data["start_line"]),
            end_line=int(data["end_line"]),
        )


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed, evidence-backed relation between two symbols."""

    src: int
    dst: int
    kind: str
    confidence: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the edge to a JSON-compatible dict."""
        return {
            "src": self.src,
            "dst": self.dst,
            "kind": self.kind,
            "confidence": self.confidence,
            "line": self.line,
        }


@dataclass(slots=True)
class Graph:
    """An in-memory graph with fast forward and reverse adjacency lookups."""

    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    _by_id: dict[int, Symbol] = field(default_factory=dict, init=False, repr=False)
    _by_name: dict[str, list[int]] = field(default_factory=dict, init=False, repr=False)
    _by_qname: dict[str, list[int]] = field(default_factory=dict, init=False, repr=False)
    _by_file: dict[str, list[int]] = field(default_factory=dict, init=False, repr=False)
    _out: dict[int, list[Edge]] = field(default_factory=dict, init=False, repr=False)
    _in: dict[int, list[Edge]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        """Build lookup structures."""
        self.reindex()

    def reindex(self) -> None:
        """Rebuild all lookup structures from the symbol and edge lists."""
        self._by_id = {symbol.id: symbol for symbol in self.symbols}
        self._by_name = {}
        self._by_qname = {}
        self._by_file = {}
        for symbol in self.symbols:
            self._by_name.setdefault(symbol.name.lower(), []).append(symbol.id)
            self._by_qname.setdefault(symbol.qualified_name.lower(), []).append(symbol.id)
            self._by_file.setdefault(symbol.file_path, []).append(symbol.id)
        self._out = {}
        self._in = {}
        for edge in self.edges:
            self._out.setdefault(edge.src, []).append(edge)
            self._in.setdefault(edge.dst, []).append(edge)

    def symbol(self, symbol_id: int) -> Symbol | None:
        """Return a symbol by id."""
        return self._by_id.get(symbol_id)

    def require_symbol(self, symbol_id: int) -> Symbol:
        """Return a symbol by id, raising ``KeyError`` when absent."""
        return self._by_id[symbol_id]

    def symbols_named(self, name: str) -> list[Symbol]:
        """Return symbols matching a simple or qualified name, case-insensitively."""
        ids = list(self._by_qname.get(name.lower(), []))
        seen = set(ids)
        for symbol_id in self._by_name.get(name.lower(), []):
            if symbol_id not in seen:
                ids.append(symbol_id)
                seen.add(symbol_id)
        return [self._by_id[i] for i in ids]

    def symbols_in_file(self, file_path: str) -> list[Symbol]:
        """Return all symbols defined in a file."""
        return [self._by_id[i] for i in self._by_file.get(file_path, [])]

    def file_symbol(self, file_path: str) -> Symbol | None:
        """Return the file-level symbol for a path."""
        for symbol_id in self._by_file.get(file_path, []):
            symbol = self._by_id[symbol_id]
            if symbol.kind is SymbolKind.FILE:
                return symbol
        return None

    def symbol_at(self, file_path: str, line: int) -> Symbol | None:
        """Return the innermost definition containing a line."""
        best: Symbol | None = None
        for symbol in self.symbols_in_file(file_path):
            if symbol.kind is SymbolKind.FILE:
                continue
            if symbol.start_line <= line <= symbol.end_line:
                if best is None or (symbol.end_line - symbol.start_line) < (best.end_line - best.start_line):
                    best = symbol
        return best or self.file_symbol(file_path)

    def out_edges(self, symbol_id: int, kinds: Iterable[str] | None = None) -> list[Edge]:
        """Return outgoing edges, optionally filtered by relation kind."""
        edges = self._out.get(symbol_id, [])
        if kinds is None:
            return list(edges)
        wanted = set(kinds)
        return [edge for edge in edges if edge.kind in wanted]

    def in_edges(self, symbol_id: int, kinds: Iterable[str] | None = None) -> list[Edge]:
        """Return incoming edges, optionally filtered by relation kind."""
        edges = self._in.get(symbol_id, [])
        if kinds is None:
            return list(edges)
        wanted = set(kinds)
        return [edge for edge in edges if edge.kind in wanted]

    def degree(self, symbol_id: int) -> int:
        """Return the total number of incoming and outgoing edges."""
        return len(self._out.get(symbol_id, [])) + len(self._in.get(symbol_id, []))

    def iter_symbols(self) -> Iterator[Symbol]:
        """Iterate over all symbols."""
        return iter(self.symbols)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the graph to a JSON-compatible dict."""
        return {
            "meta": self.meta,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "edges": [edge.to_dict() for edge in self.edges],
        }
