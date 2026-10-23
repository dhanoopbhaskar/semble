"""Graph persistence: symbols, compact edge arrays, and metadata."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from semble_graph.cache import CacheLayout, read_json, write_bytes, write_json
from semble_graph.config import EXTRACTOR_VERSION, SCHEMA_VERSION
from semble_graph.graph.model import CONFIDENCE_VALUES, EDGE_KINDS, Edge, Graph, Symbol
from semble_graph.version import __version__

SYMBOLS_FILE = "symbols.json"
EDGES_FILE = "edges.npz"
META_FILE = "graph.meta.json"
NAMES_FILE = "names.json"


def _csr(rows: list[int], columns: list[int], size: int) -> tuple[np.ndarray, np.ndarray]:
    """Build CSR indptr/indices arrays from row/column pairs."""
    order = sorted(range(len(rows)), key=lambda index: (rows[index], columns[index]))
    indptr = np.zeros(size + 1, dtype=np.int32)
    for row in rows:
        indptr[row + 1] += 1
    indptr = np.cumsum(indptr, dtype=np.int32)
    indices = np.array([columns[index] for index in order], dtype=np.int32)
    return indptr, indices


def save_graph(
    graph: Graph,
    target: Path,
    *,
    fingerprint: dict[str, object] | None = None,
    languages: dict[str, int] | None = None,
    semble_version: str | None = None,
    build_seconds: float | None = None,
) -> None:
    """Persist a graph into ``target`` (which must already exist)."""
    target.mkdir(parents=True, exist_ok=True)
    ordered = sorted(graph.edges, key=lambda edge: (edge.src, edge.dst, edge.kind, edge.line or 0))
    kind_codes = {kind: index for index, kind in enumerate(EDGE_KINDS)}
    confidence_codes = {value: index for index, value in enumerate(CONFIDENCE_VALUES)}

    src = np.array([edge.src for edge in ordered], dtype=np.int32)
    dst = np.array([edge.dst for edge in ordered], dtype=np.int32)
    kinds = np.array([kind_codes.get(edge.kind, 0) for edge in ordered], dtype=np.uint8)
    confidence = np.array([confidence_codes.get(edge.confidence, 2) for edge in ordered], dtype=np.uint8)
    lines = np.array([edge.line if edge.line is not None else -1 for edge in ordered], dtype=np.int32)

    forward_indptr, forward_indices = _csr(src.tolist(), dst.tolist(), len(graph.symbols))
    reverse_indptr, reverse_indices = _csr(dst.tolist(), src.tolist(), len(graph.symbols))

    buffer = io.BytesIO()
    np.savez_compressed(
        buffer,
        src=src,
        dst=dst,
        kind=kinds,
        confidence=confidence,
        line=lines,
        forward_indptr=forward_indptr,
        forward_indices=forward_indices,
        reverse_indptr=reverse_indptr,
        reverse_indices=reverse_indices,
    )
    write_bytes(target / EDGES_FILE, buffer.getvalue())
    write_json(target / SYMBOLS_FILE, [symbol.to_dict() for symbol in graph.symbols])

    names: dict[str, list[int]] = {}
    for symbol in graph.symbols:
        names.setdefault(symbol.name.lower(), []).append(symbol.id)
    write_json(target / NAMES_FILE, names)

    meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "sidecar_version": __version__,
        "semble_version": semble_version,
        "fingerprint": fingerprint,
        "languages": languages or {},
        "counts": {
            "files": len({symbol.file_path for symbol in graph.symbols if symbol.kind.value == "file"}),
            "symbols": len(graph.symbols),
            "edges": len(ordered),
        },
        "edge_kinds": list(EDGE_KINDS),
        "confidence_values": list(CONFIDENCE_VALUES),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "build_seconds": build_seconds,
    }
    write_json(target / META_FILE, meta)


def read_meta(layout: CacheLayout) -> dict[str, Any] | None:
    """Read current graph metadata, returning ``None`` when absent or invalid."""
    meta = read_json(layout.meta_path)
    if not isinstance(meta, dict):
        return None
    if meta.get("schema_version") != SCHEMA_VERSION:
        return None
    return meta


def load_graph(layout: CacheLayout) -> Graph | None:
    """Load the graph from a cache layout, returning ``None`` when invalid."""
    meta = read_meta(layout)
    if meta is None:
        return None
    symbols_data = read_json(layout.graph_dir / SYMBOLS_FILE)
    if not isinstance(symbols_data, list):
        return None
    try:
        symbols = [Symbol.from_dict(item) for item in symbols_data]
        with np.load(layout.graph_dir / EDGES_FILE) as data:
            src = data["src"]
            dst = data["dst"]
            kind_codes = data["kind"]
            confidence_codes = data["confidence"]
            lines = data["line"]
        kinds = meta.get("edge_kinds") or list(EDGE_KINDS)
        confidences = meta.get("confidence_values") or list(CONFIDENCE_VALUES)
        edges = [
            Edge(
                src=int(src[index]),
                dst=int(dst[index]),
                kind=str(kinds[int(kind_codes[index])]),
                confidence=str(confidences[int(confidence_codes[index])]),
                line=int(lines[index]) if int(lines[index]) >= 0 else None,
            )
            for index in range(len(src))
        ]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    return Graph(symbols=symbols, edges=edges, meta=meta)
