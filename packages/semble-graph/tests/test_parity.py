"""Contract tests: the sidecar must expose a superset of Semble's MCP and CLI surface."""

from __future__ import annotations

import argparse
import asyncio

import pytest

pytestmark = pytest.mark.contract

_SEMBLE_COMMANDS = {"search", "find-related", "file-types", "savings", "clear", "install", "uninstall"}


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {option for action in parser._actions for option in action.option_strings if option not in ("-h", "--help")}


def _positionals(parser: argparse.ArgumentParser) -> set[str]:
    return {action.dest for action in parser._actions if not action.option_strings and action.dest != "help"}


def _semble_query_parser(kind: str) -> argparse.ArgumentParser:
    from semble.cli import _add_query_args

    parser = argparse.ArgumentParser(prog=f"semble {kind}")
    if kind == "find-related":
        parser.add_argument("file_path")
        parser.add_argument("line")
    else:
        parser.add_argument("query")
    _add_query_args(parser)
    return parser


async def _mcp_schema(server: object) -> dict[str, set[str]]:
    tools = await server.list_tools()  # type: ignore[attr-defined]
    return {tool.name: set((tool.input_schema or {}).get("properties", {})) for tool in tools}


def test_mcp_schema_covers_semble() -> None:
    pytest.importorskip("mcp")
    from semble.mcp import _IndexCache
    from semble.mcp import create_server as semble_server

    from semble_graph.mcp import create_server as graph_server

    semble = asyncio.run(_mcp_schema(semble_server(_IndexCache())))
    graph = asyncio.run(_mcp_schema(graph_server()))
    for name, params in semble.items():
        assert name in graph, f"missing MCP tool {name!r}"
        assert params <= graph[name], f"{name!r} missing parameters: {sorted(params - graph[name])}"


def test_cli_commands_cover_semble() -> None:
    from semble_graph.cli import build_parser

    commands = set(_subparsers(build_parser()))
    assert _SEMBLE_COMMANDS <= commands, f"missing CLI commands: {sorted(_SEMBLE_COMMANDS - commands)}"


@pytest.mark.parametrize("kind", ["search", "find-related"])
def test_cli_query_options_cover_semble(kind: str) -> None:
    from semble_graph.cli import build_parser

    reference = _semble_query_parser(kind)
    graph = _subparsers(build_parser())[kind]
    missing_options = _options(reference) - _options(graph)
    missing_arguments = _positionals(reference) - _positionals(graph)
    assert not missing_options, f"{kind!r} missing options: {sorted(missing_options)}"
    assert not missing_arguments, f"{kind!r} missing arguments: {sorted(missing_arguments)}"


def test_cli_has_version_flag() -> None:
    from semble_graph.cli import build_parser

    assert {"-V", "--version"} <= _options(build_parser())


def test_cli_bare_invocation_runs_mcp(monkeypatch) -> None:
    import semble_graph.mcp as mcp_module
    from semble_graph import cli

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(mcp_module, "serve", lambda content=("code",): calls.append(tuple(content)))

    assert cli.main([]) == 0
    assert cli.main(["--content", "docs"]) == 0
    assert cli.main(["mcp", "--content", "code", "docs"]) == 0
    assert calls == [("code",), ("docs",), ("code", "docs")]
