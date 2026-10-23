"""TypeScript, TSX, and JavaScript extractor."""

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

_FUNCTION_DECLARATIONS = {
    "function_declaration",
    "generator_function_declaration",
    "function_signature",
    "function",
    "function_expression",
    "arrow_function",
}
_CLASS_DECLARATIONS = {"class_declaration", "abstract_class_declaration", "class"}
_TYPE_DECLARATIONS = {"interface_declaration", "type_alias_declaration", "enum_declaration"}
_METHOD_DECLARATIONS = {"method_definition", "method_signature", "abstract_method_signature", "public_field_definition"}
_STRING_TYPES = {"string", "template_string"}


def _unquote(value: str) -> str:
    """Strip matching quotes from a string literal."""
    value = value.strip()
    if len(value) >= 2 and value[0] in {"'", '"', "`"} and value[-1] == value[0]:
        return value[1:-1]
    return value


def _receiver_name(value: str) -> str | None:
    """Return the receiver of a member expression, if any."""
    receiver, _ = split_expression(value)
    return receiver


def _parse_imports(facts: FileFacts, node: Node, source: bytes) -> None:
    """Parse an import statement."""
    source_node = node.child_by_field_name("source")
    module = _unquote(text(source_node, source))
    if not module:
        return
    names: list[tuple[str, str]] = []
    clause = next((child for child in node.named_children if child.type == "import_clause"), None)
    if clause is not None:
        for child in clause.named_children:
            if child.type == "identifier":
                names.append(("default", text(child, source)))
            elif child.type == "namespace_import":
                identifier = next((item for item in child.named_children if item.type == "identifier"), None)
                if identifier is not None:
                    names.append(("*", text(identifier, source)))
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    if specifier.type != "import_specifier":
                        continue
                    imported = child_text(specifier, "name", source)
                    alias = child_text(specifier, "alias", source) or imported
                    if imported:
                        names.append((imported, alias))
    facts.imports.append(RawImport(module=module, line=line_of(node), names=names, is_from=bool(names)))


def _parse_require(facts: FileFacts, node: Node, source: bytes) -> None:
    """Treat ``require("...")`` calls as imports."""
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if function is None or arguments is None or text(function, source) != "require":
        return
    if not arguments.named_children:
        return
    argument = arguments.named_children[0]
    if argument.type not in _STRING_TYPES:
        return
    module = _unquote(text(argument, source))
    if module:
        facts.imports.append(RawImport(module=module, line=line_of(node), names=[("*", module.rsplit("/", 1)[-1])]))


def _parse_heritage(facts: FileFacts, node: Node, source: bytes, qualified: str) -> None:
    """Extract extends/implements clauses from a class."""
    heritage = next((child for child in node.named_children if child.type == "class_heritage"), None)
    if heritage is None:
        return
    for child in heritage.named_children:
        if child.type == "extends_clause":
            for value in child.named_children:
                if value.type in {"identifier", "type_identifier", "member_expression", "generic_type"}:
                    base_full = text(value, source).strip()
                    base = clean_type_name(base_full)
                    if base:
                        facts.inherits.append(
                            RawInherit(
                                owner=qualified,
                                base=base,
                                base_full=base_full,
                                relation="inherits",
                                line=line_of(value),
                            )
                        )
        elif child.type == "implements_clause":
            for value in child.named_children:
                base_full = text(value, source).strip()
                base = clean_type_name(base_full)
                if base:
                    facts.inherits.append(
                        RawInherit(
                            owner=qualified,
                            base=base,
                            base_full=base_full,
                            relation="implements",
                            line=line_of(value),
                        )
                    )
        elif child.type in {"identifier", "type_identifier", "member_expression"}:
            base_full = text(child, source).strip()
            base = clean_type_name(base_full)
            if base:
                facts.inherits.append(
                    RawInherit(
                        owner=qualified,
                        base=base,
                        base_full=base_full,
                        relation="inherits",
                        line=line_of(child),
                    )
                )


def _add_symbol(
    facts: FileFacts,
    name: str,
    kind: SymbolKind,
    node: Node,
    scope: list[RawSymbol],
    names: list[str],
    start_line: int | None = None,
) -> RawSymbol:
    """Add a symbol and return it."""
    qualified = f"{facts.path}::{'.'.join([*names, name])}"
    symbol = RawSymbol(
        name=name,
        qualified_name=qualified,
        kind=kind,
        start_line=start_line or line_of(node),
        end_line=end_line_of(node),
        parent=scope[-1].qualified_name if scope else None,
    )
    facts.add_symbol(symbol)
    return symbol


def _call(facts: FileFacts, node: Node, source: bytes, scope: list[RawSymbol], field: str) -> None:
    """Extract a call or constructor invocation."""
    target = node.child_by_field_name(field)
    if target is None:
        return
    expression = text(target, source).strip()
    if not expression or len(expression) > 160 or "\n" in expression:
        return
    if target.type not in {
        "identifier",
        "member_expression",
        "property_identifier",
        "type_identifier",
        "this",
        "super",
    }:
        return
    receiver, simple = split_expression(expression)
    if not simple:
        return
    facts.calls.append(
        RawCall(
            expression=expression,
            simple_name=simple,
            receiver=receiver,
            enclosing=scope[-1].qualified_name if scope else None,
            line=line_of(node),
        )
    )


def _walk(
    facts: FileFacts,
    node: Node,
    source: bytes,
    scope: list[RawSymbol],
    names: list[str],
    start_override: int | None = None,
) -> None:
    """Recursively walk a JavaScript-family syntax tree."""
    node_type = node.type

    if node_type in _CLASS_DECLARATIONS:
        name = child_text(node, "name", source)
        if name:
            symbol = _add_symbol(facts, name, SymbolKind.CLASS, node, scope, names, start_override)
            _parse_heritage(facts, node, source, symbol.qualified_name)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name])
        return

    if node_type in _TYPE_DECLARATIONS:
        name = child_text(node, "name", source)
        if name:
            symbol = _add_symbol(facts, name, SymbolKind.TYPE, node, scope, names, start_override)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name])
        return

    if node_type in _FUNCTION_DECLARATIONS and node_type != "arrow_function":
        name = child_text(node, "name", source)
        if name:
            symbol = _add_symbol(facts, name, SymbolKind.FUNCTION, node, scope, names, start_override)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name])
            return

    if node_type in _METHOD_DECLARATIONS:
        name = child_text(node, "name", source)
        value = node.child_by_field_name("value")
        is_function_value = value is not None and value.type in _FUNCTION_DECLARATIONS
        if name and (node_type != "public_field_definition" or is_function_value):
            symbol = _add_symbol(facts, name, SymbolKind.METHOD, node, scope, names, start_override)
            for child in node.named_children:
                _walk(facts, child, source, [*scope, symbol], [*names, name])
            return

    if node_type == "variable_declarator":
        name = child_text(node, "name", source)
        value = node.child_by_field_name("value")
        if name and value is not None:
            if value.type in _FUNCTION_DECLARATIONS:
                symbol = _add_symbol(facts, name, SymbolKind.FUNCTION, node, scope, names, start_override)
                for child in node.named_children:
                    if child is value:
                        for grandchild in child.named_children:
                            _walk(facts, grandchild, source, [*scope, symbol], [*names, name])
                    else:
                        _walk(facts, child, source, scope, names)
                return
            if name.isupper():
                _add_symbol(facts, name, SymbolKind.CONSTANT, node, scope, names, start_override)
            for child in node.named_children:
                _walk(facts, child, source, scope, names)
        return

    if node_type == "import_statement":
        _parse_imports(facts, node, source)
    elif node_type == "export_statement":
        source_node = node.child_by_field_name("source")
        if source_node is not None:
            module = _unquote(text(source_node, source))
            if module:
                facts.imports.append(
                    RawImport(module=module, line=line_of(node), names=[("*", module.rsplit("/", 1)[-1])])
                )
    elif node_type == "call_expression":
        _parse_require(facts, node, source)
        _call(facts, node, source, scope, "function")
    elif node_type == "new_expression":
        _call(facts, node, source, scope, "constructor")

    for child in node.named_children:
        _walk(facts, child, source, scope, names)


def parse(path: str, source: bytes, tree_root: Node) -> FileFacts:
    """Extract raw facts from a TypeScript/TSX/JavaScript file."""
    language = "typescript"
    if path.endswith(".tsx"):
        language = "tsx"
    elif path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        language = "javascript"
    facts = FileFacts(path=path, language=language)
    _walk(facts, tree_root, source, [], [])
    return facts
