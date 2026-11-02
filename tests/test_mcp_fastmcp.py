"""Tests for the optional FastMCP backend (``memsearch mcp --backend fastmcp``).

Skipped automatically when the third-party ``fastmcp`` package is absent, so
the default test run (stdlib backend only) needs no extra dependency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

fastmcp = pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from memsearch.config import MemSearchConfig  # noqa: E402
from memsearch.mcp_fastmcp import build_server  # noqa: E402
from memsearch.mcp_server import MCPServer  # noqa: E402


def _build(monkeypatch, **kwargs):
    # Hermetic: never pick up a real dsh-knowledge store on the test machine.
    monkeypatch.setattr("memsearch.mcp_server._auto_knowledge_db", lambda: "")
    return build_server(config=MemSearchConfig(), collection="test_collection", **kwargs)


def _tool_names(monkeypatch, **kwargs) -> list[str]:
    mcp = _build(monkeypatch, **kwargs)

    async def run() -> list[str]:
        async with Client(mcp) as client:
            tools = await client.list_tools()
        return sorted(t.name for t in tools)

    return asyncio.run(run())


def test_lists_the_same_tools_as_the_stdlib_backend(monkeypatch) -> None:
    names = _tool_names(monkeypatch)
    assert names == [
        "embed",
        "memory_expand",
        "memory_index",
        "memory_search",
        "memory_stats",
        "rerank",
    ]


def test_knowledge_search_appears_only_when_the_store_exists(monkeypatch, tmp_path: Path) -> None:
    assert "knowledge_search" not in _tool_names(monkeypatch)

    store = tmp_path / "knowledge-chunks.sqlite"
    store.write_bytes(b"")  # existence is the gate; content is not read at build time
    assert "knowledge_search" in _tool_names(monkeypatch, knowledge_db=str(store))


def test_tool_call_delegates_to_the_shared_backend(monkeypatch) -> None:
    seen: dict = {}

    def fake(self, args):
        seen.update(args)
        return f"echo:{args['query']}"

    monkeypatch.setattr(MCPServer, "tool_memory_search", fake)
    mcp = _build(monkeypatch)

    async def run() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool("memory_search", {"query": "hi", "limit": 3})
        return result.data

    assert asyncio.run(run()) == "echo:hi"
    assert seen == {"query": "hi", "limit": 3, "source_prefix": None}


def test_argument_errors_surface_as_tool_errors(monkeypatch) -> None:
    def fake(self, args):
        raise ValueError("query is required")

    monkeypatch.setattr(MCPServer, "tool_memory_search", fake)
    mcp = _build(monkeypatch)

    async def run() -> None:
        async with Client(mcp) as client:
            await client.call_tool("memory_search", {"query": "x"})

    with pytest.raises(ToolError, match="query is required"):
        asyncio.run(run())
