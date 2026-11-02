"""Unit tests for ServeService: validation, readiness, and dispatch safety.

The daemon wraps one MemSearch, but these tests never construct a real one — the
service is exercised before ``start()`` (so ``_ms`` is None) to prove readiness
gating and parameter validation, and its store-backed reads run against a stub
store so no Milvus or network is touched.
"""

import asyncio
from types import SimpleNamespace

import pytest

from memsearch.serve.protocol import INTERNAL, INVALID_PARAMS, NOT_FOUND, NOT_READY, RpcError
from memsearch.serve.service import ServeService


def make_cfg(**over) -> SimpleNamespace:
    """A minimal MemSearchConfig-shaped stand-in (attribute access only)."""
    return SimpleNamespace(
        milvus=SimpleNamespace(collection="c", uri="~/.memsearch/milvus.db", token=""),
        embedding=SimpleNamespace(provider="openai", model="m", batch_size=0, base_url="", api_key=""),
        chunking=SimpleNamespace(max_chunk_size=1500, overlap_lines=2),
        indexing=SimpleNamespace(ignore_files=[], exclude=[]),
        watch=SimpleNamespace(debounce_ms=500),
        reranker=SimpleNamespace(model=over.get("reranker_model", "")),
    )


def make_service(**over) -> ServeService:
    return ServeService(make_cfg(**over), ["S:\\apps\\llm\\agentwork\\.memsearch\\memory"])


def test_search_requires_query():
    svc = make_service()
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.search({"query": ""}))
    assert exc.value.code == INVALID_PARAMS


def test_search_not_ready_before_start():
    # _require() gates reads on a live MemSearch, which only exists after start().
    svc = make_service()
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.search({"query": "hi"}))
    assert exc.value.code == NOT_READY


def test_top_k_rejects_nonpositive():
    svc = make_service()
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.search({"query": "hi", "top_k": 0}))
    assert exc.value.code == INVALID_PARAMS


def test_index_requires_existing_paths():
    svc = make_service()
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.index({"paths": ["S:\\definitely\\not\\here\\xyz.md"]}))
    assert exc.value.code == NOT_FOUND


def test_ping_and_status_without_network():
    # ping/status read daemon bookkeeping only, so they answer pre-start.
    svc = make_service()
    assert svc.ping({}) == {"pong": True, "ready": False}
    st = svc.status({})
    assert st["ready"] is False
    assert st["collection"] == "c"
    assert "S:\\apps\\llm\\agentwork\\.memsearch\\memory" in st["paths"]


def test_dispatch_rejects_lifecycle_and_private():
    # "start" is lifecycle-only and must not be reachable from a socket payload.
    svc = make_service()
    for bad in ("start", "dispatch", "_require", "_fanout"):
        with pytest.raises(RpcError) as exc:
            asyncio.run(svc.dispatch(bad, {}))
        assert exc.value.code == INTERNAL


def test_dispatch_awaitable_and_sync_paths():
    # dispatch awaits coroutine handlers and passes sync results straight through.
    svc = make_service()
    assert asyncio.run(svc.dispatch("ping", {}))["pong"] is True


def test_expand_rejects_missing_chunk():
    svc = make_service()
    # Pretend a live MemSearch whose store finds nothing.
    store = SimpleNamespace(query=lambda **kw: [])
    svc._ms = SimpleNamespace(store=store, _embedder=None)
    svc._shutting_down = False
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.expand({"chunk_id": "nope"}))
    assert exc.value.code == NOT_FOUND


def test_expand_missing_chunk_id():
    svc = make_service()
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.expand({}))
    assert exc.value.code == INVALID_PARAMS


def test_rerank_requires_model_or_config():
    svc = make_service()  # reranker.model == ""
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.rerank({"query": "q", "documents": ["a"]}))
    assert exc.value.code == INVALID_PARAMS


def test_embed_requires_texts():
    svc = make_service()
    with pytest.raises(RpcError) as exc:
        asyncio.run(svc.embed({"texts": []}))
    assert exc.value.code == INVALID_PARAMS


def test_stats_uses_store_count(monkeypatch):
    svc = make_service()
    store = SimpleNamespace(count=lambda: 7, indexed_sources=lambda: ["x.md", "y.md"])
    svc._ms = SimpleNamespace(store=store)
    svc._shutting_down = False
    out = asyncio.run(svc.stats({}))
    assert out["total_chunks"] == 7
    assert out["sources"] == 2
    assert out["source_list"] == ["x.md", "y.md"]


def test_expand_section_reads_windowed_and_full(monkeypatch):
    # A real source file so read_utf8_text_replace and extract_section run.
    import os
    import tempfile

    from memsearch.serve import service as svc_mod

    text = "# Title\n\nline one\nline two\n\n# Next\n\nbody\n"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        # heading_level 1 at the "# Title" line -> full section
        res = svc_mod._expand_section({"source": path, "start_line": 1, "heading_level": 1, "chunk_hash": "h"})
        assert res["first_line"] == 1
        assert "line one" in res["content"]
        # windowed n=1 around start_line 3 (0-based idx 2)
        res2 = svc_mod._expand_windowed({"source": path, "start_line": 3, "end_line": 3}, 1)
        assert res2["first_line"] == 2
        assert res2["last_line"] == 4
        assert "line one" in res2["content"]
        assert "line two" in res2["content"]
    finally:
        os.unlink(path)


def test_rerank_maps_scores_through_owned_index_key(monkeypatch):
    # Stub the blocking rerank() so no HTTP/local model runs; verify the doc:i
    # keys are decoded back to caller positions.
    svc = make_service(reranker_model="remote:fake")
    import memsearch.reranker as rr_mod

    def fake_rerank(query, candidates, *, model_name, top_k):
        # return reversed order with scores
        rev = list(reversed(candidates))
        for i, r in enumerate(rev):
            r["score"] = round(1.0 - i * 0.2, 2)
        return rev

    monkeypatch.setattr(rr_mod, "rerank", fake_rerank)
    out = asyncio.run(svc.rerank({"query": "q", "documents": ["aa", "bb", "cc"]}))
    assert out["model"] == "remote:fake"
    assert [d["document"] for d in out["results"]] == ["cc", "bb", "aa"]
    assert out["results"][0]["index"] == 2
