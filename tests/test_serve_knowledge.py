"""Tests for the daemon's knowledge_search method and the MCP daemon routing.

KnowledgeStore is monkeypatched at its defining module, so no real SQLite or
embedding provider is touched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import memsearch.knowledge_store as ks_module
from memsearch.config import MemSearchConfig
from memsearch.serve.protocol import INVALID_PARAMS, NOT_FOUND, RpcError
from memsearch.serve.service import ServeService


def make_cfg(**over) -> SimpleNamespace:
    return SimpleNamespace(
        milvus=SimpleNamespace(collection="c", uri="~/.memsearch/milvus.db", token=""),
        embedding=SimpleNamespace(provider="openai", model="m", batch_size=0, base_url="", api_key=""),
        chunking=SimpleNamespace(max_chunk_size=1500, overlap_lines=2),
        indexing=SimpleNamespace(ignore_files=[], exclude=[]),
        watch=SimpleNamespace(debounce_ms=500),
        reranker=SimpleNamespace(model=over.get("reranker_model", "remote:m")),
    )


class FakeEmbedder:
    model_name = "m"
    dimension = 2

    async def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeKBStore:
    # Class-level constant (read-only); RUF012 wants the intent explicit.
    HITS: tuple = ({"score": 0.9, "doc_id": "d1", "heading": "H", "text": "kb body"},)

    def __init__(self, *a, **k) -> None:
        self.search_args: dict = {}

    def search(self, query, *, limit, base_id, embed_fn, rerank_fn):
        self.search_args = {"query": query, "limit": limit, "base_id": base_id}
        vectors = embed_fn(["probe"])
        assert vectors == [[0.1, 0.2]]
        scores = rerank_fn("q", ["d0"])
        assert scores is None or isinstance(scores, list)
        return list(self.HITS)

    def close(self) -> None:
        pass


@pytest.fixture
def service(monkeypatch, tmp_path) -> ServeService:
    kb = tmp_path / "knowledge-chunks.sqlite"
    kb.write_bytes(b"")
    svc = ServeService(make_cfg(), ["S:\\apps\\llm\\agentwork"], knowledge_db=str(kb))
    svc._ms = SimpleNamespace(_embedder=FakeEmbedder())
    monkeypatch.setattr(ks_module, "KnowledgeStore", FakeKBStore)
    return svc


def test_knowledge_search_returns_hits(service: ServeService) -> None:
    data = asyncio.run(service.knowledge_search({"query": "kb query", "limit": 3}))
    assert data["count"] == 1
    assert data["hits"][0]["doc_id"] == "d1"


def test_knowledge_search_passes_params(service: ServeService, monkeypatch) -> None:
    store = FakeKBStore()
    monkeypatch.setattr(ks_module, "KnowledgeStore", lambda *a, **k: store)
    asyncio.run(service.knowledge_search({"query": "q", "limit": 2, "base_id": "b1"}))
    assert store.search_args == {"query": "q", "limit": 2, "base_id": "b1"}


def test_knowledge_search_requires_query(service: ServeService) -> None:
    with pytest.raises(RpcError) as exc:
        asyncio.run(service.knowledge_search({"query": ""}))
    assert exc.value.code == INVALID_PARAMS


def test_knowledge_search_without_store_is_not_found() -> None:
    svc = ServeService(make_cfg(), ["S:\\apps\\llm\\agentwork"], knowledge_db="")
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.knowledge_search({"query": "q"}))
    assert exc.value.code == NOT_FOUND


def test_knowledge_search_in_client_methods() -> None:
    assert "knowledge_search" in ServeService.CLIENT_METHODS


# ------------------------------------------------------------------ MCP routing

from memsearch.mcp_server import MCPServer  # noqa: E402


class FakeDaemonClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def endpoint(self) -> str:
        return "fake-endpoint"

    def is_alive(self) -> bool:
        return True

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        if method == "knowledge_search":
            return {
                "query": params["query"],
                "count": 1,
                "hits": [{"score": 0.8, "doc_id": "d9", "heading": "KH", "text": "kb hit body"}],
            }
        raise AssertionError(f"unexpected method {method}")


def test_mcp_knowledge_search_routes_through_daemon(monkeypatch) -> None:
    fake = FakeDaemonClient()
    monkeypatch.setattr("memsearch.mcp_server.ThinClient", lambda **kw: fake)
    server = MCPServer(config=MemSearchConfig(), collection="c", daemon=True)
    out = server.tool_knowledge_search({"query": "hello", "limit": 2})
    assert "score=0.800000" in out and "kb hit body" in out
    assert fake.calls[0] == ("knowledge_search", {"query": "hello", "limit": 2, "base_id": None})


def test_mcp_knowledge_search_no_match(monkeypatch) -> None:
    fake = FakeDaemonClient()
    monkeypatch.setattr("memsearch.mcp_server.ThinClient", lambda **kw: fake)

    async def empty(method, params, timeout=None):
        fake.calls.append((method, params))
        return {"query": params["query"], "count": 0, "hits": []}

    fake.call = empty  # type: ignore[method-assign]
    server = MCPServer(config=MemSearchConfig(), collection="c", daemon=True)
    assert server.tool_knowledge_search({"query": "hello"}) == "(no match)"
