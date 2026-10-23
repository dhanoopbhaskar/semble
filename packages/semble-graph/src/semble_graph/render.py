"""JSON and human-readable rendering."""

from __future__ import annotations

from typing import Any

import orjson


def render_json(value: Any) -> str:
    """Render a value as deterministic JSON."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2).decode("utf-8")


def _symbol_line(symbol: dict[str, Any]) -> str:
    location = f"{symbol.get('file_path', '?')}:{symbol.get('start_line', '?')}"
    return f"{symbol.get('kind', '?')} {symbol.get('qualified_name', symbol.get('name', '?'))} ({location})"


def render_text(value: Any, kind: str | None = None) -> str:
    """Render a tool payload as readable text."""
    if kind == "symbol":
        return _render_symbol(value)
    if kind == "neighbors":
        return _render_neighbors(value)
    if kind == "impact":
        return _render_impact(value)
    if kind == "overview":
        return _render_overview(value)
    if kind == "search":
        return _render_search(value)
    if kind == "status":
        return _render_status(value)
    if isinstance(value, dict) and "error" in value:
        return f"error: {value['error']}"
    return render_json(value)


def _render_symbol(value: dict[str, Any]) -> str:
    lines: list[str] = []
    matches = value.get("matches", [])
    if not matches:
        return f"No symbol found for {value.get('query', '')!r}."
    for match in matches:
        lines.append(_symbol_line(match))
        relations = match.get("relations") or {}
        for role in (
            "callers",
            "callees",
            "importers",
            "imports",
            "inherits",
            "inherited_by",
            "implements",
            "implemented_by",
            "tested_by",
        ):
            items = relations.get(role) or []
            if not items:
                continue
            labels = [
                f"{(item.get('symbol') or {}).get('qualified_name', '?')}[{item.get('confidence', '?')}]"
                for item in items[:10]
            ]
            suffix = f" (+{len(items) - 10} more)" if len(items) > 10 else ""
            lines.append(f"  {role}: " + ", ".join(labels) + suffix)
    return "\n".join(lines)


def _render_neighbors(value: dict[str, Any]) -> str:
    if value.get("error"):
        return f"error: {value['error']}"
    lines = [f"neighbors (depth={value.get('depth')}, relation={value.get('relation') or 'all'}):"]
    for item in value.get("neighbors", []):
        symbol = item.get("symbol") or {}
        lines.append(
            f"  [d{item.get('depth')}] {item.get('relation')} {symbol.get('qualified_name', '?')} "
            f"({symbol.get('file_path', '?')}:{symbol.get('start_line', '?')}) "
            f"[{item.get('confidence', '?')}]"
        )
    if len(lines) == 1:
        lines.append("  (none)")
    return "\n".join(lines)


def _render_impact(value: dict[str, Any]) -> str:
    if value.get("error"):
        return f"error: {value['error']}"
    lines = [f"impact (total={value.get('total', 0)}, depth={value.get('depth')}):"]
    for group in value.get("results", []):
        lines.append(f"  depth {group.get('depth')}:")
        for item in group.get("symbols", []):
            symbol = item.get("symbol") or {}
            lines.append(
                f"    {symbol.get('qualified_name', '?')} via {item.get('relation')} [{item.get('confidence', '?')}]"
            )
    if len(lines) == 1:
        lines.append("  (none)")
    return "\n".join(lines)


def _render_overview(value: dict[str, Any]) -> str:
    lines = ["overview:"]
    counts = value.get("counts") or {}
    if counts:
        lines.append(f"  files={counts.get('files')} symbols={counts.get('symbols')} edges={counts.get('edges')}")
    if value.get("languages"):
        lines.append("  languages: " + ", ".join(f"{name}={count}" for name, count in value["languages"].items()))
    lines.append("  core symbols:")
    for symbol in value.get("core_symbols", []):
        lines.append(f"    {_symbol_line(symbol)} degree={symbol.get('degree')}")
    lines.append("  entry points:")
    for symbol in value.get("entry_points", []):
        lines.append(f"    {_symbol_line(symbol)}")
    return "\n".join(lines)


def _render_search(value: dict[str, Any]) -> str:
    if value.get("error"):
        return f"error: {value['error']}"
    lines = [
        f"graph_search {value.get('query', '')!r} "
        f"(intent={value.get('intent')}, graph={value.get('graph_status')}, results={value.get('count', 0)}):"
    ]
    for result in value.get("results", []):
        via = result.get("via", "semantic")
        relation = result.get("relation") or ""
        lines.append(
            f"  {result.get('score')} {result.get('file_path')}:{result.get('start_line')} "
            f"{result.get('qualified_name')} via={via}{' relation=' + relation if relation else ''}"
        )
    return "\n".join(lines)


def _render_status(value: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {item}" for key, item in value.items() if key != "fingerprint")
