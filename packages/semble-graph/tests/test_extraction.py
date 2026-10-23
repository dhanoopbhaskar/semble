from __future__ import annotations

from pathlib import Path

from semble_graph.graph.extract import extract_path

FIXTURES = Path(__file__).parent / "fixtures" / "sample"


def _parse(relative: str, language: str) -> object:
    path = FIXTURES / relative
    return extract_path(Path(relative), path.read_bytes(), language)


def test_python_symbols_imports_calls_inheritance() -> None:
    facts = _parse("src/payments/service.py", "python")
    names = {symbol.qualified_name for symbol in facts.symbols}
    assert "src/payments/service.py::PaymentService" in names
    assert "src/payments/service.py::PaymentService.charge" in names
    assert "src/payments/service.py::create_service" in names
    assert any(item.module == "gateway" for item in facts.imports)
    assert {item.simple_name for item in facts.calls} >= {"authorize", "charge", "PaymentService"}


def test_python_test_detection_and_decorated_definitions(tmp_path: Path) -> None:
    source = b"""
import functools

@functools.cache
def cached(x):
    return helper(x)

class TestThing:
    def test_it(self):
        return cached(1)
"""
    facts = extract_path(Path("tests/test_x.py"), source, "python")
    cached = next(symbol for symbol in facts.symbols if symbol.name == "cached")
    assert cached.start_line == 4
    assert "tests/test_x.py::TestThing.test_it" in {symbol.qualified_name for symbol in facts.symbols}


def test_typescript_symbols_imports_calls_and_heritage() -> None:
    facts = _parse("web/client.ts", "typescript")
    names = {symbol.qualified_name for symbol in facts.symbols}
    assert "web/client.ts::Client" in names
    assert "web/client.ts::Client.run" in names
    assert "web/client.ts::retry" in names
    assert any(item.module == "./helper" for item in facts.imports)
    assert any(item.simple_name == "helper" for item in facts.calls)


def test_go_symbols_imports_and_calls() -> None:
    facts = _parse("go/service.go", "go")
    names = {symbol.qualified_name for symbol in facts.symbols}
    assert "go/service.go::Store" in names
    assert "go/service.go::Store.Save" in names
    assert "go/service.go::NewStore" in names
    assert any(item.module == "fmt" for item in facts.imports)
    assert any(item.expression == "fmt.Println" for item in facts.calls)


def test_rust_symbols_imports_and_calls() -> None:
    facts = _parse("rs/lib.rs", "rust")
    names = {symbol.qualified_name for symbol in facts.symbols}
    assert "rs/lib.rs::Engine" in names
    assert "rs/lib.rs::Engine.run" in names
    assert any(item.module == "util" for item in facts.imports)
    assert any(item.expression == "util::helper" for item in facts.calls)


def test_java_interface_extends_heritage(tmp_path: Path) -> None:
    source = b"package com.x;\npublic interface SubGateway extends Gateway, Other { }\n"
    facts = extract_path(Path("com/x/SubGateway.java"), source, "java")
    relations = {(item.relation, item.base) for item in facts.inherits}
    assert relations == {("inherits", "Gateway"), ("inherits", "Other")}


def test_java_symbols_imports_calls_and_heritage() -> None:
    facts = _parse("java/com/example/Service.java", "java")
    names = {symbol.qualified_name for symbol in facts.symbols}
    assert "java/com/example/Service.java::Service" in names
    assert "java/com/example/Service.java::Service.run" in names
    assert any(item.module == "com.example.Helper" for item in facts.imports)
    assert any(item.expression == "Helper.help" for item in facts.calls)
    relations = {(item.relation, item.base) for item in facts.inherits}
    assert ("inherits", "Base") in relations
    assert ("implements", "Api") in relations
