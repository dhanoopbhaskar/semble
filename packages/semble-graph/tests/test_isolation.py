from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "semble_graph"


def _imports_semble(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "semble" or alias.name.startswith("semble."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "semble" or module.startswith("semble."):
                return True
    return False


def test_only_compat_imports_semble() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "compat.py":
            continue
        if _imports_semble(path):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"Only compat.py may import Semble; offenders: {offenders}"


def test_public_exports() -> None:
    import semble_graph

    assert set(semble_graph.__all__) == {"Edge", "Graph", "Symbol", "SymbolKind", "__version__"}
