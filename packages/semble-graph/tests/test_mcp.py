from __future__ import annotations

import asyncio

import pytest


def test_mcp_tool_names() -> None:
    pytest.importorskip("mcp")
    from semble_graph.mcp import create_server

    server = create_server(include_search=False)

    async def _list() -> list[str]:
        tools = await server.list_tools()
        return [tool.name for tool in tools]

    names = asyncio.run(_list())
    assert names
    assert all(name.startswith("graph_") for name in names), names
    assert "search" not in names


def test_mcp_combined_mode_adds_search() -> None:
    pytest.importorskip("mcp")
    from semble_graph.mcp import create_server

    server = create_server(include_search=True)

    async def _list() -> list[str]:
        tools = await server.list_tools()
        return [tool.name for tool in tools]

    names = asyncio.run(_list())
    assert "search" in names
    assert "find_related" in names


def test_mcp_graph_symbol_call(sample_repo, tmp_path, monkeypatch) -> None:
    pytest.importorskip("mcp")
    import json

    from semble_graph.mcp import create_server

    monkeypatch.setenv("SEMBLE_GRAPH_CACHE_DIR", str(tmp_path / "cache"))
    server = create_server(include_search=False)

    async def _call() -> object:
        return await server.call_tool("graph_symbol", {"name": "PaymentService", "repo": str(sample_repo)})

    result = asyncio.run(_call())
    payload = json.loads(result.content[0].text)
    assert payload["count"] == 1
    assert payload["matches"][0]["qualified_name"].endswith("PaymentService")


def test_mcp_errors_are_structured(tmp_path) -> None:
    pytest.importorskip("mcp")
    import json

    from semble_graph.mcp import create_server

    server = create_server(include_search=False)

    async def _call() -> object:
        return await server.call_tool("graph_symbol", {"name": "X", "repo": str(tmp_path / "missing")})

    result = asyncio.run(_call())
    payload = json.loads(result.content[0].text)
    assert payload["graph_status"] == "unavailable"
    assert "error" in payload
