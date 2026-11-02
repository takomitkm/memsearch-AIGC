"""Tests for the MCP --daemon routing (serve plan step 7).

The daemon client is a fake, so nothing here opens Milvus or talks to a real
daemon: the point is that store-backed MCP tools delegate to the daemon and
keep their output format, that knowledge callbacks ride the daemon's warm
model, and that a dead daemon surfaces as a clean tool error.
"""

from __future__ import annotations

import json

import pytest

import memsearch.serve.client as client_mod
from memsearch.config import MemSearchConfig
from memsearch.mcp_server import MCPServer
from memsearch.serve.client import DaemonDownError


class FakeDaemonClient:
    """Records calls; returns the shapes ServeService actually produces."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.calls: list[tuple[str, dict]] = []

    @property
    def endpoint(self) -> str:
        return "fake-endpoint"

    def is_alive(self) -> bool:
        return self.alive

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        if method == "search":
            return {"results": [{"score": 1.0, "source": "s.md", "heading": "H", "content": params["query"]}]}
        if method == "stats":
            return {"collection": "c", "total_chunks": 7, "sources": 1, "source_list": ["s.md"]}
        if method == "expand":
            return {
                "source": "s.md",
                "chunk_hash": "abc",
                "first_line": 1,
                "last_line": 3,
                "heading": "H",
                "content": "body",
            }
        if method == "embed":
            return {
                "model": "m",
                "count": len(params["texts"]),
                "dimension": 2,
                "vectors": [{"norm": 1.0, "embedding": [0.1, 0.2]} for _ in params["texts"]],
            }
        if method == "rerank":
            return {
                "model": "remote:m",
                "results": [
                    {"index": i, "score": 0.9 - i * 0.1, "document": d} for i, d in enumerate(params["documents"])
                ],
            }
        if method == "index":
            return {"paths": params["paths"], "indexed_chunks": 3, "indexed_files": 1, "failed": 0}
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture
def daemon_server(monkeypatch) -> tuple[MCPServer, FakeDaemonClient]:
    fake = FakeDaemonClient()
    monkeypatch.setattr(client_mod, "ThinClient", lambda **kw: fake)
    return MCPServer(config=MemSearchConfig(), collection="c", daemon=True), fake


def test_search_routes_and_keeps_format(daemon_server) -> None:
    server, fake = daemon_server
    out = server.tool_memory_search({"query": "hi"})
    assert "hi" in out and "s.md" in out
    assert fake.calls[0][0] == "search"
    assert fake.calls[0][1] == {"query": "hi", "top_k": 5, "source_prefix": None}


def test_stats_routes_and_keeps_format(daemon_server) -> None:
    server, fake = daemon_server
    out = server.tool_memory_stats({})
    assert "Total indexed chunks: 7" in out and "s.md" in out
    assert fake.calls[0][0] == "stats"


def test_expand_routes_and_keeps_format(daemon_server) -> None:
    server, fake = daemon_server
    out = server.tool_memory_expand({"chunk_id": "abc"})
    assert "Source: s.md (lines 1-3)" in out and "\nbody" in out
    assert fake.calls[0][0] == "expand"


def test_embed_routes_and_keeps_format(daemon_server) -> None:
    server, fake = daemon_server
    out = json.loads(server.tool_embed({"texts": ["a"], "include_vector": False}))
    assert out["model"] == "m" and out["count"] == 1
    assert fake.calls[0][0] == "embed"


def test_rerank_routes_and_keeps_format(daemon_server) -> None:
    server, fake = daemon_server
    out = server.tool_rerank({"query": "q", "documents": ["d0", "d1"]})
    assert out.splitlines()[0].startswith("0. score=0.900000")
    assert "d0" in out.splitlines()[0]
    assert fake.calls[0][0] == "rerank"


def test_index_routes_and_keeps_format(daemon_server) -> None:
    server, fake = daemon_server
    out = server.tool_memory_index({"paths": ["x.md"], "force": False})
    assert out == "indexed 3 chunk(s) from x.md"
    assert fake.calls[0][1]["paths"] == ["x.md"]


def test_knowledge_callbacks_ride_the_daemon(daemon_server) -> None:
    server, fake = daemon_server
    vectors = server._embed_texts(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert fake.calls[0][0] == "embed"
    assert fake.calls[0][1]["include_vector"] is True


def test_store_access_is_blocked_in_daemon_mode(daemon_server) -> None:
    server, _fake = daemon_server
    with pytest.raises(RuntimeError, match="owned by 'memsearch serve'"):
        server._memsearch()
    with pytest.raises(RuntimeError, match="owned by 'memsearch serve'"):
        server._get_store()


def test_daemon_down_at_construction(monkeypatch) -> None:
    monkeypatch.setattr(client_mod, "ThinClient", lambda **kw: FakeDaemonClient(alive=False))
    with pytest.raises(DaemonDownError):
        MCPServer(config=MemSearchConfig(), collection="c", daemon=True)


def test_daemon_dying_midrun_is_a_tool_error_not_a_crash(daemon_server) -> None:
    server, fake = daemon_server
    fake.alive = False

    def broken(method, params, timeout=None):
        raise DaemonDownError("fake-endpoint", None)

    fake.call = broken  # type: ignore[method-assign]
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "memory_search", "arguments": {"query": "hi"}},
        }
    )
    result = reply["result"]
    assert result["isError"] is True
    assert "memsearch serve start" in result["content"][0]["text"]


def test_default_construction_stays_direct(monkeypatch) -> None:
    # No daemon flag: no client is created and the local path is untouched.
    server = MCPServer(config=MemSearchConfig(), collection="c")
    assert server._daemon_client is None
