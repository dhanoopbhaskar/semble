"""Java extractor."""

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

_CLASS_NODES = {
    "class_declaration": SymbolKind.CLASS,
    "record_declaration": SymbolKind.CLASS,
    "enum_declaration": SymbolKind.TYPE,
    "interface_declaration": SymbolKind.TYPE,
    "annotation_type_declaration": SymbolKind.TYPE,
}
_METHOD_NODES = {"method_declaration", "constructor_declaration"}
_HERITAGE_FIELDS = ("superclass", "interfaces", "extends_interfaces")


def _parse_imports(facts: FileFacts, node: Node, source: bytes) -> None:
    """Parse a Java import declaration."""
    statement = " ".join(text(node, source).split()).rstrip(";")
    if not statement.startswith("import "):
        return
    body = statement[7:].strip()
    if body.startswith("static "):
        body = body[7:].strip()
    if not body:
        return
    last = body.rsplit(".", 1)[-1]
    if last == "*":
        facts.imports.append(RawImport(module=body[:-2], line=line_of(node), names=[("*", "*")], is_from=True))
    else:
        facts.imports.append(RawImport(module=body, line=line_of(node), names=[(last, last)], is_from=True))


def _parse_heritage(facts: FileFacts, node: Node, source: bytes, qualified: str) -> None:
    """Extract superclass and interface clauses."""
    for field in _HERITAGE_FIELDS:
        heritage = node.child_by_field_name(field)
        if heritage is None:
            continue
        for child in heritage.named_children:
            if child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
                base_full = text(child, source).strip()
                base = clean_type_name(base_full)
                if base:
                    relation = "inherits" if field == "superclass" else "implements"
                    facts.inherits.append(
                        RawInherit(
                            owner=qualified,
                            base=base,
                            base_full=base_full,
                            relation=relation,
                            line=line_of(child),
                        )
                    )
            elif child.type == "type_list":
                for item in child.named_children:
                    base_full = text(item, source).strip()
                    base = clean_type_name(base_full)
                    if base:
                        facts.inherits.append(
                            RawInherit(
                                owner=qualified,
                                base=base,
                                base_full=base_full,
                                relation="implements",
                                line=line_of(item),
                            )
                        )


def _add_symbol(
    facts: FileFacts,
    name: str,
    kind: SymbolKind,
    node: Node,
    scope: list[RawSymbol],
    names: list[str],
) -> RawSymbol:
    """Add a Java symbol and return it."""
    qualified = f"{facts.path}::{'.'.join([*names, name])}"
    symbol = RawSymbol(
        name=name,
        qualified_name=qualified,
        kind=kind,
        start_line=line_of(node),
        end_line=end_line_of(node),
        parent=scope[-1].qualified_name if scope else None,
    )
    facts.add_symbol(symbol)
    return symbol


def _call(facts: FileFacts, node: Node, source: bytes, scope: list[RawSymbol]) -> None:
    """Extract a method invocation or constructor call."""
    if node.type == "object_creation_expression":
        type_node = node.child_by_field_name("type")
        expression = text(type_node, source).strip()
    else:
        name = child_text(node, "name", source)
        object_node = node.child_by_field_name("object")
        object_text = text(object_node, source).strip() if object_node is not None else ""
        expression = f"{object_text}.{name}" if object_text and name else name
    expression = expression.split("<", 1)[0].strip()
    if not expression or expression in {"this", "super"} or len(expression) > 160:
        return
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


def _walk(facts: FileFacts, node: Node, source: bytes, scope: list[RawSymbol], names: list[str]) -> None:
    """Recursively walk a Java syntax tree."""
    node_type = node.type

    if node_type in _CLASS_NODES:
        name = child_text(node, "name", source)
        if name:
            symbol = _add_symbol(facts, name, _CLASS_NODES[node_type], node, scope, names)
            _parse_heritage(facts, node, source, symbol.qualified_name)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name])
        return

    if node_type in _METHOD_NODES:
        name = child_text(node, "name", source)
        if name:
            symbol = _add_symbol(facts, name, SymbolKind.METHOD, node, scope, names)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name])
        return

    if node_type == "field_declaration":
        for child in node.named_children:
            if child.type != "variable_declarator":
                continue
            name = child_text(child, "name", source)
            if name and name.isupper():
                _add_symbol(facts, name, SymbolKind.CONSTANT, child, scope, names)

    elif node_type == "import_declaration":
        _parse_imports(facts, node, source)
    elif node_type == "method_invocation":
        _call(facts, node, source, scope)
    elif node_type == "object_creation_expression":
        _call(facts, node, source, scope)

    for child in node.named_children:
        _walk(facts, child, source, scope, names)


def parse(path: str, source: bytes, tree_root: Node) -> FileFacts:
    """Extract raw facts from a Java file."""
    facts = FileFacts(path=path, language="java")
    _walk(facts, tree_root, source, [], [])
    return facts
