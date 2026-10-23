"""Global name, import, and call resolution."""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from semble_graph.graph.extractors.base import FileFacts, RawImport
from semble_graph.graph.model import Confidence, Edge, Graph, Symbol, SymbolKind

_JS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_CALL_RELATIONS = {"calls", "references"}


@dataclass(slots=True)
class _Resolver:
    """Resolve raw file facts into a graph."""

    facts: list[FileFacts]
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    _qname: dict[str, int] = field(default_factory=dict)
    _file_symbols: dict[str, int] = field(default_factory=dict)
    _by_name: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    _by_file: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    _bindings: dict[str, dict[str, list[int]]] = field(default_factory=lambda: defaultdict(dict))
    _paths: list[str] = field(default_factory=list)
    _dirs: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    _edge_keys: set[tuple[int, int, str]] = field(default_factory=set)
    _out_adj: dict[int, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    _parents: dict[int, int] = field(default_factory=dict)

    def build(self) -> Graph:
        """Create the global graph."""
        self._paths = sorted(facts.path for facts in self.facts)
        for path in self._paths:
            self._dirs[posixpath.dirname(path)].append(path)
        self._add_symbols()
        self._resolve_imports()
        self._resolve_inheritance()
        self._resolve_calls()
        graph = Graph(
            symbols=self.symbols,
            edges=self.edges,
            meta={
                "symbols": len(self.symbols),
                "edges": len(self.edges),
                "files": len(self.facts),
            },
        )
        return graph

    # ------------------------------------------------------------------
    # Symbol table
    # ------------------------------------------------------------------

    def _add_symbol(self, symbol: Symbol) -> None:
        self.symbols.append(symbol)
        self._qname[symbol.qualified_name] = symbol.id
        self._by_name[symbol.name].append(symbol.id)
        self._by_file[symbol.file_path].append(symbol.id)
        if symbol.kind is SymbolKind.FILE:
            self._file_symbols[symbol.file_path] = symbol.id

    def _add_edge(
        self,
        src: int,
        dst: int,
        kind: str,
        confidence: str = Confidence.EXTRACTED.value,
        line: int | None = None,
    ) -> None:
        key = (src, dst, kind)
        if src == dst or key in self._edge_keys:
            return
        self._edge_keys.add(key)
        edge = Edge(src=src, dst=dst, kind=kind, confidence=confidence, line=line)
        self.edges.append(edge)
        self._out_adj[src].append(edge)

    def _add_symbols(self) -> None:
        next_id = 0
        for facts in self.facts:
            end_line = max((symbol.end_line for symbol in facts.symbols), default=1)
            file_symbol = Symbol(
                id=next_id,
                name=PurePosixPath(facts.path).name,
                qualified_name=facts.path,
                kind=SymbolKind.FILE,
                file_path=facts.path,
                start_line=1,
                end_line=end_line,
            )
            self._add_symbol(file_symbol)
            next_id += 1

        for facts in self.facts:
            file_id = self._file_symbols[facts.path]
            for raw in facts.symbols:
                symbol = Symbol(
                    id=next_id,
                    name=raw.name,
                    qualified_name=raw.qualified_name,
                    kind=raw.kind,
                    file_path=facts.path,
                    start_line=raw.start_line,
                    end_line=raw.end_line,
                )
                self._add_symbol(symbol)
                next_id += 1
                self._add_edge(file_id, symbol.id, "defines")
                if raw.parent and raw.parent in self._qname:
                    parent_id = self._qname[raw.parent]
                    self._parents[symbol.id] = parent_id
                    self._add_edge(parent_id, symbol.id, "contains")

    # ------------------------------------------------------------------
    # Import target resolution
    # ------------------------------------------------------------------

    def _path_candidates(self, stems: list[str]) -> list[str]:
        """Return existing paths matching any stem and common extensions."""
        found: list[str] = []
        for stem in stems:
            stem = posixpath.normpath(stem)
            while stem.startswith("./"):
                stem = stem[2:]
            stem = stem.lstrip("/")
            if not stem or stem == ".":
                continue
            for candidate in (
                stem,
                f"{stem}.py",
                f"{stem}.pyi",
                f"{stem}/__init__.py",
                *(f"{stem}{extension}" for extension in _JS_EXTENSIONS),
                *(f"{stem}/index{extension}" for extension in _JS_EXTENSIONS),
                f"{stem}.java",
                f"{stem}.go",
                f"{stem}.rs",
                f"{stem}/mod.rs",
            ):
                if candidate in self._file_symbols:
                    found.append(candidate)
        return list(dict.fromkeys(found))

    def _suffix_candidates(self, suffixes: list[str]) -> list[str]:
        """Find existing files ending with any suffix, preferring shortest paths."""
        matches: list[str] = []
        for path in self._paths:
            for suffix in suffixes:
                suffix = suffix.lstrip("./")
                if path == suffix or path.endswith("/" + suffix):
                    matches.append(path)
                    break
        return sorted(dict.fromkeys(matches), key=lambda item: (item.count("/"), item))

    def _python_targets(self, facts: FileFacts, imp: RawImport) -> list[str]:
        module = imp.module.replace(".", "/")
        if imp.level:
            base = posixpath.dirname(facts.path)
            for _ in range(imp.level - 1):
                base = posixpath.dirname(base)
            stem = posixpath.join(base, module) if module else base
            targets = self._path_candidates([stem])
            if targets:
                return targets
            if module:
                return self._suffix_candidates([f"{module}.py", f"{module}/__init__.py"])
            return []
        targets = self._path_candidates([module])
        if targets:
            return targets
        return self._suffix_candidates([f"{module}.py", f"{module}/__init__.py"])

    def _js_targets(self, facts: FileFacts, imp: RawImport) -> list[str]:
        module = imp.module
        if module.startswith("."):
            stem = posixpath.normpath(posixpath.join(posixpath.dirname(facts.path), module))
            return self._path_candidates([stem])
        if module.startswith("/"):
            return self._path_candidates([module.lstrip("/")])
        cleaned = module[2:] if module.startswith("@/") else module
        targets = self._path_candidates([cleaned])
        if targets:
            return targets
        suffixes = [f"{cleaned}{extension}" for extension in _JS_EXTENSIONS]
        suffixes.extend(f"{cleaned}/index{extension}" for extension in _JS_EXTENSIONS)
        return self._suffix_candidates(suffixes)

    def _go_targets(self, imp: RawImport) -> list[str]:
        parts = [part for part in imp.module.split("/") if part]
        if not parts:
            return []
        for count in range(len(parts), 0, -1):
            suffix = "/".join(parts[-count:])
            dirs = sorted(
                (directory for directory in self._dirs if directory == suffix or directory.endswith("/" + suffix)),
                key=lambda item: (item.count("/"), item),
            )
            if dirs:
                return sorted(self._dirs[dirs[0]])
        return []

    def _rust_targets(self, facts: FileFacts, imp: RawImport) -> list[str]:
        module = imp.module
        for prefix in ("::", "crate::", "self::"):
            if module.startswith(prefix):
                module = module[len(prefix) :]
        base = posixpath.dirname(facts.path)
        if module.startswith("super::"):
            module = module[len("super::") :]
            base = posixpath.dirname(base)
        parts = [part for part in module.split("::") if part]
        if not parts:
            return []
        for count in range(len(parts), 0, -1):
            stem = posixpath.join(base, *parts[:count]) if base else "/".join(parts[:count])
            targets = self._path_candidates([stem])
            if targets:
                return targets
            suffix = "/".join(parts[:count])
            targets = self._suffix_candidates([f"{suffix}.rs", f"{suffix}/mod.rs"])
            if targets:
                return targets
        return []

    def _java_targets(self, imp: RawImport) -> list[str]:
        module = imp.module
        parts = [part for part in module.split(".") if part]
        for count in range(len(parts), 0, -1):
            stem = "/".join(parts[:count])
            targets = self._path_candidates([stem])
            if targets:
                return targets
            targets = self._suffix_candidates([f"{stem}.java"])
            if targets:
                return targets
        return []

    def _module_targets(self, facts: FileFacts, imp: RawImport) -> list[str]:
        if facts.language == "python":
            return self._python_targets(facts, imp)
        if facts.language in {"typescript", "tsx", "javascript"}:
            return self._js_targets(facts, imp)
        if facts.language == "go":
            return self._go_targets(imp)
        if facts.language == "rust":
            return self._rust_targets(facts, imp)
        if facts.language == "java":
            return self._java_targets(imp)
        return []

    def _symbols_in_files(self, paths: list[str], name: str | None = None) -> list[int]:
        result: list[int] = []
        for path in paths:
            for symbol_id in self._by_file.get(path, []):
                symbol = self.symbols[symbol_id]
                if symbol.kind is SymbolKind.FILE:
                    continue
                if name is None or symbol.name == name:
                    result.append(symbol_id)
        return result

    def _resolve_imports(self) -> None:
        for facts in self.facts:
            file_id = self._file_symbols[facts.path]
            for imp in facts.imports:
                targets = self._module_targets(facts, imp)
                for path in targets:
                    self._add_edge(file_id, self._file_symbols[path], "imports", line=imp.line)

                if not imp.names:
                    if imp.alias and targets:
                        self._bind(facts.path, imp.alias, [self._file_symbols[path] for path in targets])
                    elif targets and facts.language in {"python", "go", "rust", "java"}:
                        # ``import a.b`` / ``use a::b`` also introduces ``b`` as a local name.
                        local = imp.module.replace("::", ".").replace("/", ".").rsplit(".", 1)[-1]
                        self._bind(facts.path, local, [self._file_symbols[path] for path in targets])
                    continue

                for imported, alias in imp.names:
                    if imported == "*":
                        for path in targets:
                            self._add_edge(file_id, self._file_symbols[path], "imports_from", line=imp.line)
                        if targets:
                            self._bind(facts.path, alias, [self._file_symbols[path] for path in targets])
                        continue

                    matches = self._symbols_in_files(targets, imported)
                    if not matches and not targets and facts.language != "python":
                        # Named imports such as ``import {x} from "./side"`` still need a module target.
                        pass
                    if not matches and facts.language == "python" and imp.is_from:
                        submodule = f"{imp.module}.{imported}" if imp.module else imported
                        sub_imp = RawImport(
                            module=submodule,
                            line=imp.line,
                            alias=None,
                            is_from=False,
                            level=imp.level,
                        )
                        sub_targets = self._python_targets(facts, sub_imp)
                        for path in sub_targets:
                            self._add_edge(file_id, self._file_symbols[path], "imports", line=imp.line)
                        matches = self._symbols_in_files(sub_targets, imported)
                        targets = sub_targets or targets

                    if matches:
                        for symbol_id in matches:
                            self._add_edge(file_id, symbol_id, "imports_from", line=imp.line)
                        self._bind(facts.path, alias, matches[:1])
                    elif targets:
                        file_ids = [self._file_symbols[path] for path in targets]
                        self._bind(facts.path, alias, file_ids)
                    elif facts.language in {"go", "rust", "java"}:
                        self._bind(facts.path, alias, [])

    def _bind(self, file_path: str, alias: str, ids: list[int]) -> None:
        if ids:
            self._bindings[file_path][alias] = ids

    # ------------------------------------------------------------------
    # Inheritance
    # ------------------------------------------------------------------

    def _lookup(self, facts: FileFacts, name: str, receiver: str | None = None) -> tuple[list[int], str]:
        """Resolve a simple name in the context of a file.

        Returns candidates plus a confidence label.
        """
        file_ids = self._by_file.get(facts.path, [])
        direct = [symbol_id for symbol_id in file_ids if self.symbols[symbol_id].name == name]
        if direct:
            return direct, Confidence.EXTRACTED.value

        if receiver:
            bound = self._bindings[facts.path].get(receiver, [])
            scoped = self._symbols_in_files(
                [self.symbols[item].file_path for item in bound if self.symbols[item].kind is SymbolKind.FILE], name
            )
            if scoped:
                return scoped, Confidence.EXTRACTED.value
            nested: list[int] = []
            for container in bound:
                nested.extend(
                    edge.dst
                    for edge in self._edges_for(container)
                    if edge.kind == "contains" and self.symbols[edge.dst].name == name
                )
            if nested:
                return nested, Confidence.EXTRACTED.value

        bound = self._bindings[facts.path].get(name, [])
        if bound:
            return bound, Confidence.EXTRACTED.value

        directory = posixpath.dirname(facts.path)
        siblings = self._dirs.get(directory, [])
        sibling_matches = self._symbols_in_files(siblings, name)
        if len(sibling_matches) == 1:
            return sibling_matches, Confidence.EXTRACTED.value

        global_matches = self._by_name.get(name, [])
        global_matches = [item for item in global_matches if self.symbols[item].kind is not SymbolKind.FILE]
        if len(global_matches) == 1:
            return global_matches, Confidence.INFERRED.value
        if len(global_matches) > 1:
            return sorted(global_matches), Confidence.AMBIGUOUS.value
        if sibling_matches:
            return sorted(sibling_matches), Confidence.AMBIGUOUS.value
        return [], Confidence.AMBIGUOUS.value

    def _edges_for(self, symbol_id: int) -> list[Edge]:
        return self._out_adj.get(symbol_id, [])

    def _resolve_inheritance(self) -> None:
        for facts in self.facts:
            for inherit in facts.inherits:
                owner = self._qname.get(inherit.owner)
                if owner is None:
                    owner = self._file_symbols.get(facts.path)
                if owner is None:
                    continue
                candidates, confidence = self._lookup(facts, inherit.base)
                if not candidates:
                    continue
                target = candidates[0]
                if len(candidates) > 1:
                    confidence = Confidence.AMBIGUOUS.value
                self._add_edge(owner, target, inherit.relation, confidence, inherit.line)

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    def _candidates_for_call(self, facts: FileFacts, call: object) -> tuple[list[int], str]:
        """Resolve a call site using the four resolution tiers."""
        simple = getattr(call, "simple_name", "")
        receiver = getattr(call, "receiver", None)
        enclosing = getattr(call, "enclosing", None)
        if not simple:
            return [], Confidence.AMBIGUOUS.value

        file_ids = self._by_file.get(facts.path, [])
        if receiver:
            if receiver in {"self", "this", "cls"} and enclosing:
                parent_id = self._parents.get(self._qname.get(enclosing, -1))
                while parent_id is not None:
                    nested = [
                        edge.dst
                        for edge in self._edges_for(parent_id)
                        if edge.kind == "contains" and self.symbols[edge.dst].name == simple
                    ]
                    if nested:
                        return nested, Confidence.EXTRACTED.value
                    parent_id = self._parents.get(parent_id)
            else:
                bound = self._bindings[facts.path].get(receiver, [])
                if bound:
                    scoped = self._symbols_in_files(
                        [self.symbols[item].file_path for item in bound if self.symbols[item].kind is SymbolKind.FILE],
                        simple,
                    )
                    if scoped:
                        return scoped, Confidence.EXTRACTED.value
                    nested = []
                    for container in bound:
                        nested.extend(
                            edge.dst
                            for edge in self._edges_for(container)
                            if edge.kind == "contains" and self.symbols[edge.dst].name == simple
                        )
                    if nested:
                        return nested, Confidence.EXTRACTED.value
                class_matches = [
                    symbol_id
                    for symbol_id in file_ids
                    if self.symbols[symbol_id].name == receiver
                    and self.symbols[symbol_id].kind in {SymbolKind.CLASS, SymbolKind.TYPE}
                ]
                for class_id in class_matches:
                    nested = [
                        edge.dst
                        for edge in self._edges_for(class_id)
                        if edge.kind == "contains" and self.symbols[edge.dst].name == simple
                    ]
                    if nested:
                        return nested, Confidence.EXTRACTED.value

        direct = [symbol_id for symbol_id in file_ids if self.symbols[symbol_id].name == simple]
        if direct:
            return direct, Confidence.EXTRACTED.value

        candidates, confidence = self._lookup(facts, simple, receiver=receiver)
        return candidates, confidence

    def _resolve_calls(self) -> None:
        for facts in self.facts:
            file_alias = self._file_symbols[facts.path]
            for call in facts.calls:
                source = self._qname.get(call.enclosing) if call.enclosing else None
                source = source or file_alias
                candidates, confidence = self._candidates_for_call(facts, call)
                if not candidates:
                    continue
                if len(candidates) > 1:
                    confidence = Confidence.AMBIGUOUS.value
                target = candidates[0]
                if target == source:
                    continue
                self._add_edge(source, target, "calls", confidence, call.line)
                source_symbol = self.symbols[source]
                target_symbol = self.symbols[target]
                if self._is_test(facts, source_symbol) and not self._is_test(facts, target_symbol):
                    self._add_edge(source, target, "tests", confidence, call.line)

    def _is_test(self, facts: FileFacts, symbol: Symbol) -> bool:
        from semble_graph.graph.extractors.base import is_test_name, is_test_path

        return is_test_path(symbol.file_path) or is_test_name(symbol.name)


def resolve(facts: list[FileFacts]) -> Graph:
    """Resolve file facts into a global graph."""
    ordered = sorted(facts, key=lambda item: item.path)
    return _Resolver(facts=ordered).build()
