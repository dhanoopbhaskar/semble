"""Python extractor."""

from __future__ import annotations

import re

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

_DEFINITIONS = {"function_definition", "class_definition"}
_SPLIT_NAMES = re.compile(r",\s*(?![^()]*\))")


def _parse_imports(facts: FileFacts, node: Node, source: bytes) -> None:
    """Parse ``import`` and ``from ... import ...`` statements."""
    statement = " ".join(text(node, source).split())
    line = line_of(node)
    if statement.startswith("from "):
        try:
            prefix, names_part = statement[5:].split(" import ", 1)
        except ValueError:
            return
        prefix = prefix.strip()
        if prefix == "__future__" or "<" in prefix:
            return
        level = len(prefix) - len(prefix.lstrip("."))
        module = prefix.lstrip(".")
        names_part = names_part.strip().strip("()")
        names: list[tuple[str, str]] = []
        for part in _SPLIT_NAMES.split(names_part):
            part = part.strip()
            if not part or part == "*":
                if part == "*":
                    names.append(("*", "*"))
                continue
            pieces = part.split(" as ")
            imported = pieces[0].strip()
            alias = pieces[1].strip() if len(pieces) > 1 else imported
            if imported:
                names.append((imported, alias))
        facts.imports.append(RawImport(module=module, line=line, names=names, is_from=True, level=level))
        return

    if statement.startswith("import "):
        body = statement[7:]
        for part in body.split(","):
            part = part.strip()
            if not part:
                continue
            pieces = part.split(" as ")
            module = pieces[0].strip()
            alias = pieces[1].strip() if len(pieces) > 1 else None
            if module:
                facts.imports.append(RawImport(module=module, line=line, alias=alias))


def _call_from_node(node: Node, facts: FileFacts, source: bytes, scope: list[RawSymbol]) -> RawCall | None:
    """Build a raw call from a call expression."""
    function = node.child_by_field_name("function")
    if function is None:
        return None
    if function.type not in {"identifier", "attribute"}:
        return None
    expression = text(function, source).strip()
    if not expression or expression in {"super", "print"}:
        return None
    receiver, simple = split_expression(expression)
    return RawCall(
        expression=expression,
        simple_name=simple,
        receiver=receiver,
        enclosing=scope[-1].qualified_name if scope else None,
        line=line_of(node),
    )


def _add_definition(
    facts: FileFacts,
    node: Node,
    source: bytes,
    scope: list[RawSymbol],
    names: list[str],
    start_line: int,
) -> RawSymbol | None:
    """Create a function/class symbol and return it."""
    name = child_text(node, "name", source)
    if not name:
        return None
    is_class = node.type == "class_definition"
    if is_class:
        kind = SymbolKind.CLASS
    else:
        parent_class = bool(scope) and scope[-1].kind is SymbolKind.CLASS
        kind = SymbolKind.METHOD if parent_class else SymbolKind.FUNCTION
    qualified = f"{facts.path}::{'.'.join([*names, name])}"
    symbol = RawSymbol(
        name=name,
        qualified_name=qualified,
        kind=kind,
        start_line=start_line,
        end_line=end_line_of(node),
        parent=scope[-1].qualified_name if scope else None,
    )
    facts.add_symbol(symbol)

    if is_class:
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is not None:
            for argument in superclasses.named_children:
                if argument.type in {"keyword_argument", "comment"}:
                    continue
                base_full = text(argument, source).strip()
                base = clean_type_name(base_full)
                if base:
                    facts.inherits.append(
                        RawInherit(
                            owner=qualified,
                            base=base,
                            base_full=base_full,
                            relation="inherits",
                            line=line_of(argument),
                        )
                    )
    return symbol


def _walk(
    facts: FileFacts,
    node: Node,
    source: bytes,
    scope: list[RawSymbol],
    names: list[str],
    start_override: int | None = None,
) -> None:
    """Recursively walk a Python syntax tree."""
    node_type = node.type

    if node_type == "decorated_definition":
        definition = next((child for child in node.named_children if child.type in _DEFINITIONS), None)
        for child in node.named_children:
            if child is definition:
                if definition is not None:
                    _walk(facts, definition, source, scope, names, start_override=line_of(node))
            else:
                _walk(facts, child, source, scope, names)
        return

    if node_type in _DEFINITIONS:
        start_line = start_override or line_of(node)
        symbol = _add_definition(facts, node, source, scope, names, start_line)
        if symbol is not None:
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, symbol.name])
        return

    if node_type == "import_statement" or node_type == "import_from_statement":
        _parse_imports(facts, node, source)

    elif node_type == "call":
        call = _call_from_node(node, facts, source, scope)
        if call is not None:
            facts.calls.append(call)

    elif node_type == "assignment":
        left = node.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            name = text(left, source)
            parent_is_class = bool(scope) and scope[-1].kind is SymbolKind.CLASS
            if name.isupper() or (parent_is_class and name):
                qualified = f"{facts.path}::{'.'.join([*names, name])}"
                facts.add_symbol(
                    RawSymbol(
                        name=name,
                        qualified_name=qualified,
                        kind=SymbolKind.CONSTANT,
                        start_line=line_of(node),
                        end_line=end_line_of(node),
                        parent=scope[-1].qualified_name if scope else None,
                    )
                )

    elif node_type == "type_alias_statement":
        left = node.named_children[0] if node.named_children else None
        name = text(left, source).strip() if left is not None else ""
        if name:
            qualified = f"{facts.path}::{'.'.join([*names, name])}"
            facts.add_symbol(
                RawSymbol(
                    name=name,
                    qualified_name=qualified,
                    kind=SymbolKind.TYPE,
                    start_line=line_of(node),
                    end_line=end_line_of(node),
                    parent=scope[-1].qualified_name if scope else None,
                )
            )

    for child in node.named_children:
        if child.type in {"string", "comment"}:
            continue
        _walk(facts, child, source, scope, names)


def parse(path: str, source: bytes, tree_root: Node) -> FileFacts:
    """Extract raw facts from a Python file."""
    facts = FileFacts(path=path, language="python")
    _walk(facts, tree_root, source, [], [])
    return facts
