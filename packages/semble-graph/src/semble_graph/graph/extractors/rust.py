"""Rust extractor."""

from __future__ import annotations

from tree_sitter import Node

from semble_graph.graph.extractors.base import (
    FileFacts,
    RawCall,
    RawImport,
    RawInherit,
    RawSymbol,
    child_text,
    clean_type_name,
    end_line_of,
    line_of,
    split_expression,
    text,
)
from semble_graph.graph.model import SymbolKind

_TYPE_ITEMS = {"struct_item", "enum_item", "union_item", "trait_item", "type_item"}
_CONST_ITEMS = {"const_item", "static_item"}


def _parse_use(facts: FileFacts, node: Node, source: bytes) -> None:
    """Parse a Rust ``use`` declaration."""
    statement = " ".join(text(node, source).split()).rstrip(";")
    if not statement.startswith("use "):
        return
    body = statement[4:].strip()
    if body.startswith("::"):
        body = body[2:]
    if " as " in body:
        path, alias = body.rsplit(" as ", 1)
        last = path.rsplit("::", 1)[-1].strip()
        facts.imports.append(
            RawImport(module=path.strip(), line=line_of(node), names=[(last, alias.strip())], is_from=True)
        )
        return
    if "{" in body:
        prefix, rest = body.split("{", 1)
        prefix = prefix.rstrip(":").strip()
        items = rest.rstrip("}").split(",")
        names: list[tuple[str, str]] = []
        for item in items:
            item = item.strip()
            if not item:
                continue
            if " as " in item:
                path_part, alias_part = item.rsplit(" as ", 1)
            else:
                path_part, alias_part = item, None
            last = path_part.strip().rsplit("::", 1)[-1]
            names.append((last, alias_part.strip() if alias_part else last))
        if prefix:
            facts.imports.append(RawImport(module=prefix, line=line_of(node), names=names, is_from=True))
        return
    last = body.rsplit("::", 1)[-1].strip()
    facts.imports.append(RawImport(module=body.strip(), line=line_of(node), names=[(last, last)], is_from=True))


def _add_symbol(
    facts: FileFacts,
    name: str,
    kind: SymbolKind,
    node: Node,
    names: list[str],
    parent: str | None = None,
) -> RawSymbol:
    """Add a Rust symbol and return it."""
    qualified = f"{facts.path}::{'.'.join([*names, name])}"
    symbol = RawSymbol(
        name=name,
        qualified_name=qualified,
        kind=kind,
        start_line=line_of(node),
        end_line=end_line_of(node),
        parent=parent,
    )
    facts.add_symbol(symbol)
    return symbol


def _walk(
    facts: FileFacts,
    node: Node,
    source: bytes,
    scope: list[RawSymbol],
    names: list[str],
    in_type_body: bool = False,
) -> None:
    """Recursively walk a Rust syntax tree."""
    node_type = node.type

    if node_type == "impl_item":
        type_node = node.child_by_field_name("type")
        type_name = clean_type_name(text(type_node, source)) if type_node is not None else ""
        trait_node = node.child_by_field_name("trait")
        if type_name and trait_node is not None:
            trait_full = text(trait_node, source).strip()
            trait_name = clean_type_name(trait_full)
            if trait_name:
                facts.inherits.append(
                    RawInherit(
                        owner=f"{facts.path}::{type_name}",
                        base=trait_name,
                        base_full=trait_full,
                        relation="implements",
                        line=line_of(node),
                    )
                )
        body_names = [*names, type_name] if type_name else names
        for child in node.named_children:
            _walk(facts, child, source, scope, body_names, in_type_body=bool(type_name))
        return

    if node_type in {"mod_item", "trait_item"}:
        name = child_text(node, "name", source)
        if name:
            kind = SymbolKind.MODULE if node_type == "mod_item" else SymbolKind.TYPE
            symbol = _add_symbol(facts, name, kind, node, names)
            if node_type == "mod_item":
                facts.imports.append(RawImport(module=name, line=line_of(node)))
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name], in_type_body=node_type == "trait_item")
        return

    if node_type in _TYPE_ITEMS:
        name = child_text(node, "name", source)
        if name:
            symbol = _add_symbol(facts, name, SymbolKind.TYPE, node, names)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name], in_type_body=True)
        return

    if node_type in {"function_item", "function_signature_item"}:
        name = child_text(node, "name", source)
        if name:
            parent = f"{facts.path}::{'.'.join(names)}" if in_type_body and names else None
            kind = SymbolKind.METHOD if in_type_body else SymbolKind.FUNCTION
            symbol = _add_symbol(facts, name, kind, node, names, parent)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name], in_type_body=False)
        return

    if node_type in _CONST_ITEMS:
        name = child_text(node, "name", source)
        if name:
            _add_symbol(facts, name, SymbolKind.CONSTANT, node, names)
        return

    if node_type == "use_declaration":
        _parse_use(facts, node, source)
    elif node_type in {"call_expression", "macro_invocation"}:
        function = node.child_by_field_name("function")
        if function is None and node_type == "macro_invocation":
            function = node.child_by_field_name("macro")
        if function is not None:
            expression = text(function, source).strip().split("<", 1)[0]
            if expression and len(expression) <= 160 and "\n" not in expression:
                receiver, simple = split_expression(expression)
                facts.calls.append(
                    RawCall(
                        expression=expression,
                        simple_name=simple,
                        receiver=receiver,
                        enclosing=scope[-1].qualified_name if scope else None,
                        line=line_of(node),
                    )
                )

    for child in node.named_children:
        _walk(facts, child, source, scope, names, in_type_body=in_type_body)


def parse(path: str, source: bytes, tree_root: Node) -> FileFacts:
    """Extract raw facts from a Rust file."""
    facts = FileFacts(path=path, language="rust")
    _walk(facts, tree_root, source, [], [])
    return facts
