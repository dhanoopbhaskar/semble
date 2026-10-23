from __future__ import annotations

import asyncio

import pytest


def _names(server: object) -> list[str]:
    async def _list() -> list[str]:
        tools = await server.list_tools()  # type: ignore[attr-defined]
        return [tool.name for tool in tools]

    return asyncio.run(_list())


def test_mcp_tool_names_expose_semble_surface() -> None:
    pytest.importorskip("mcp")
    from semble_graph.mcp import create_server

    names = _names(create_server())
    assert names
    assert all(name.startswith("graph_") for name in names if name not in {"search", "find_related"}), names
    assert {"search", "find_related"} <= set(names)


def test_mcp_graph_symbol_call(sample_repo, tmp_path, monkeypatch) -> None:
    pytest.importorskip("mcp")
    import json

    from semble_graph.mcp import create_server

    monkeypatch.setenv("SEMBLE_GRAPH_CACHE_DIR", str(tmp_path / "cache"))
    server = create_server()

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

    server = create_server()

    async def _call() -> object:
        return await server.call_tool("graph_symbol", {"name": "X", "repo": str(tmp_path / "missing")})

    result = asyncio.run(_call())
    payload = json.loads(result.content[0].text)
    assert payload["graph_status"] == "unavailable"
    assert "error" in payload


def test_mcp_search_matches_semble_payload(sample_repo, tmp_path, monkeypatch) -> None:
    pytest.importorskip("mcp")
    import json

    from semble_graph.mcp import create_server

    server = create_server()

    async def _call() -> object:
        return await server.call_tool(
            "search",
            {"query": "payment gateway", "repo": str(sample_repo), "top_k": 2, "max_snippet_lines": 0},
        )

    result = asyncio.run(_call())
    payload = json.loads(result.content[0].text)
    assert payload["query"] == "payment gateway"
    assert payload["results"]
    assert {"file_path", "start_line", "end_line", "score"} <= set(payload["results"][0])
    assert "content" not in payload["results"][0]
