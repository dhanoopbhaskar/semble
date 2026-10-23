"""Go extractor."""

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


def _unquote(value: str) -> str:
    """Strip matching quotes from a Go string literal."""
    value = value.strip()
    if len(value) >= 2 and value[0] in {'"', "`"} and value[-1] == value[0]:
        return value[1:-1]
    return value


def _parse_imports(facts: FileFacts, node: Node, source: bytes) -> None:
    """Parse Go import declarations."""
    specs = [node] if node.type == "import_spec" else []
    if not specs:
        for child in node.named_children:
            if child.type == "import_spec":
                specs.append(child)
            elif child.type == "import_spec_list":
                specs.extend(item for item in child.named_children if item.type == "import_spec")
    for spec in specs:
        path_node = spec.child_by_field_name("path")
        module = _unquote(text(path_node, source))
        if not module:
            continue
        alias = child_text(spec, "name", source) or None
        facts.imports.append(RawImport(module=module, line=line_of(spec), alias=alias))


def _receiver_type(node: Node, source: bytes) -> str | None:
    """Extract the receiver type name of a Go method."""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    for child in receiver.named_children:
        if child.type != "parameter_declaration":
            continue
        type_node = child.child_by_field_name("type")
        if type_node is not None:
            name = clean_type_name(text(type_node, source))
            if name:
                return name
    return None


def _add_symbol(
    facts: FileFacts,
    name: str,
    kind: SymbolKind,
    node: Node,
    names: list[str],
    parent: str | None = None,
) -> RawSymbol:
    """Add a symbol and return it."""
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


def _parse_embedded(facts: FileFacts, node: Node, source: bytes, owner: str) -> None:
    """Treat embedded struct fields as inheritance edges."""
    if node.type != "struct_type":
        return
    for field_list in node.named_children:
        if field_list.type != "field_declaration_list":
            continue
        for field in field_list.named_children:
            if field.type != "field_declaration":
                continue
            if field.child_by_field_name("name") is not None:
                continue
            type_node = field.child_by_field_name("type")
            if type_node is None:
                continue
            base_full = text(type_node, source).strip()
            base = clean_type_name(base_full)
            if base:
                facts.inherits.append(
                    RawInherit(owner=owner, base=base, base_full=base_full, relation="inherits", line=line_of(field))
                )


def _call(facts: FileFacts, node: Node, source: bytes, scope: list[RawSymbol]) -> None:
    """Extract a Go call expression."""
    function = node.child_by_field_name("function")
    if function is None:
        return
    if function.type == "identifier":
        expression = text(function, source)
    elif function.type == "selector_expression":
        expression = text(function, source)
    elif function.type == "parenthesized_expression":
        return
    else:
        return
    expression = expression.strip()
    if not expression:
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
    """Recursively walk a Go syntax tree."""
    node_type = node.type

    if node_type == "type_declaration":
        for spec in node.named_children:
            if spec.type != "type_spec":
                continue
            name = child_text(spec, "name", source)
            if not name:
                continue
            type_node = spec.child_by_field_name("type")
            symbol = _add_symbol(facts, name, SymbolKind.TYPE, spec, names)
            if type_node is not None:
                _parse_embedded(facts, type_node, source, symbol.qualified_name)
        return

    if node_type == "function_declaration":
        name = child_text(node, "name", source)
        if name:
            symbol = _add_symbol(facts, name, SymbolKind.FUNCTION, node, names)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name])
        return

    if node_type == "method_declaration":
        name = child_text(node, "name", source)
        receiver = _receiver_type(node, source)
        if name and receiver:
            parent_qname = f"{facts.path}::{receiver}"
            symbol = _add_symbol(facts, name, SymbolKind.METHOD, node, [*names, receiver], parent_qname)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, receiver, name])
        return

    if node_type == "import_declaration":
        _parse_imports(facts, node, source)
    elif node_type in {"const_declaration", "var_declaration"}:
        for spec in node.named_children:
            if spec.type not in {"const_spec", "var_spec"}:
                continue
            name = child_text(spec, "name", source)
            if name and (node_type == "const_declaration" or name.isupper()):
                _add_symbol(facts, name, SymbolKind.CONSTANT, spec, names)
    elif node_type == "call_expression":
        _call(facts, node, source, scope)

    for child in node.named_children:
        _walk(facts, child, source, scope, names)


def parse(path: str, source: bytes, tree_root: Node) -> FileFacts:
    """Extract raw facts from a Go file."""
    facts = FileFacts(path=path, language="go")
    _walk(facts, tree_root, source, [], [])
    return facts
