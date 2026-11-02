"""Business operations exposed by the serve daemon, transport-agnostic.

ServeService wraps one :class:`~memsearch.core.MemSearch` (one collection) and
presents every operation a client can request — search, index, stats, expand,
embed, rerank, watch — as a method returning plain data (dicts/lists), never a
formatted string or a socket. The server's transport adapters encode these
results into protocol responses; nothing here knows about JSON-lines, sockets,
or HTTP, which is what lets the thin client, the MCP server, and an external
harness share one implementation.

Concurrency model
-----------------
Milvus Lite allows a single writer per collection and blocking calls (a remote
``rerank`` HTTP request, the watcher's start/stop) must not stall the loop, so
operation methods are coroutines that run blocking work on worker threads
(``asyncio.to_thread``) and await the parts of :class:`MemSearch` that are
already async. Index requests take an internal lock so two clients can never
race concurrent upserts on one collection.

The file watcher fires its callbacks on a background thread; the bridge here
pushes events onto per-subscriber :class:`asyncio.Queue` objects via
``loop.call_soon_threadsafe``, so a ``watch_subscribe`` stream is delivered on
the loop that awaits it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from pathlib import Path
from typing import Any

from .protocol import (
    INDEX_FAILED,
    INTERNAL,
    INVALID_PARAMS,
    NOT_FOUND,
    NOT_READY,
    SHUTTING_DOWN,
    RpcError,
)


class ServeService:
    """The daemon's operation surface for one collection.

    Parameters
    ----------
    cfg:
        A resolved :class:`~memsearch.config.MemSearchConfig` (embedding, Milvus,
        chunking, reranker, watch settings). Read at start; not reloaded live.
    paths:
        Root paths to index and watch. ``index`` with no explicit paths reindexes
        these; the watcher monitors them.
    """

    def __init__(self, cfg: Any, paths: list[str]) -> None:
        from ..config import memsearch_kwargs_from_config

        self._cfg = cfg
        self._paths = [str(p) for p in paths]
        self._kwargs = memsearch_kwargs_from_config(cfg)
        self._ms: Any = None
        self._watcher: Any = None
        self._subscribers: set[asyncio.Queue] = set()
        self._index_lock = asyncio.Lock()
        self._ready = False
        self._shutting_down = False
        self._started_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the store, perform the initial index, and start the watcher."""
        from ..core import MemSearch

        self._loop = asyncio.get_running_loop()
        self._ms = MemSearch(self._paths, **self._kwargs)
        await self._ms.index_with_report()
        self._watcher = self._ms.watch(
            on_event=self._on_watch_event,
            on_error=self._on_watch_error,
            debounce_ms=self._cfg.watch.debounce_ms,
        )
        self._started_at = time.time()
        self._ready = True

    async def shutdown(self, params: dict[str, Any] | None = None) -> None:
        """Stop the watcher and release the store. Idempotent.

        Accepts an ignored ``params`` so it can be invoked through
        :meth:`dispatch` like every other client method, and also called
        directly by the daemon's signal/shutdown path.
        """
        self._ready = False
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._watcher is not None:
            await asyncio.to_thread(self._watcher.stop)
            self._watcher = None
        if self._ms is not None:
            self._ms.close()
            self._ms = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------
    # Operations (dispatch table keys == protocol METHODS)
    # ------------------------------------------------------------------

    def ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "ready": self._ready}

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        uptime = time.time() - self._started_at if self._ready else 0.0
        return {
            "ready": self._ready,
            "shutting_down": self._shutting_down,
            "collection": self._cfg.milvus.collection,
            "paths": self._paths,
            "uptime_seconds": round(uptime, 3),
            "watching": self._watcher is not None,
            "subscribers": len(self._subscribers),
        }

    async def search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            raise RpcError("query is required", INVALID_PARAMS)
        top_k = _positive_int(params.get("top_k"), default=10, name="top_k")
        source_prefix = params.get("source_prefix") or None
        ms = self._require()
        results = await ms.search(query, top_k=top_k, source_prefix=source_prefix)
        return {"results": results, "count": len(results)}

    async def index(self, params: dict[str, Any]) -> dict[str, Any]:
        force = bool(params.get("force"))
        raw = params.get("paths")
        if raw is None:
            # Reindex the daemon's own roots on its long-lived MemSearch.
            ms = self._require()
            async with self._index_lock:
                try:
                    report = await ms.index_with_report(force=force)
                except Exception as exc:
                    raise RpcError(f"index failed: {type(exc).__name__}: {exc}", INDEX_FAILED) from exc
            return _index_report_dict(self._paths, report, chunks=None)
        if isinstance(raw, str):
            raw = [raw]
        paths = [str(p) for p in raw if str(p).strip()]
        if not paths:
            raise RpcError("paths is required (string or list of strings)", INVALID_PARAMS)
        missing = [p for p in paths if not Path(p).expanduser().exists()]
        if missing:
            raise RpcError(f"path not found: {', '.join(missing)}", NOT_FOUND)
        # Paths outside the watched roots run on a short-lived MemSearch (the
        # daemon's watcher keeps its own store lifetime), mirroring how the MCP
        # server indexes ad-hoc paths with the same config-derived kwargs.
        from ..core import MemSearch

        kwargs = {**self._kwargs, "paths": [str(Path(p).expanduser().resolve()) for p in paths]}
        ms = MemSearch(**kwargs)
        try:
            async with self._index_lock:
                try:
                    count = await ms.index(force=force)
                except Exception as exc:
                    raise RpcError(f"index failed: {type(exc).__name__}: {exc}", INDEX_FAILED) from exc
        finally:
            ms.close()
        return {"paths": paths, "indexed_chunks": count, "failed": 0}

    async def stats(self, params: dict[str, Any]) -> dict[str, Any]:
        store = self._require().store
        count = await asyncio.to_thread(store.count)
        try:
            sources = sorted(await asyncio.to_thread(store.indexed_sources))
        except Exception:  # pragma: no cover - backend dependent
            sources = []
        return {
            "collection": self._cfg.milvus.collection,
            "total_chunks": count,
            "sources": len(sources),
            "source_list": sources[:20],
        }

    async def expand(self, params: dict[str, Any]) -> dict[str, Any]:
        chunk_hash = str(params.get("chunk_id") or params.get("chunk_hash") or "").strip()
        if not chunk_hash:
            raise RpcError("chunk_id is required", INVALID_PARAMS)
        escaped = chunk_hash.replace("\\", "\\\\").replace('"', '\\"')
        store = self._require().store
        chunks = await asyncio.to_thread(store.query, filter_expr=f'chunk_hash == "{escaped}"')
        if not chunks:
            raise RpcError(f"chunk not found: {chunk_hash}", NOT_FOUND)
        chunk = chunks[0]
        lines_ctx = params.get("lines")
        if lines_ctx is not None:
            # lines=0 is the CLI --no-section view: the chunk block alone.
            n = _non_negative_int(lines_ctx, default=1, name="lines")
            return await asyncio.to_thread(_expand_windowed, chunk, n)
        return await asyncio.to_thread(_expand_section, chunk)

    async def embed(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("texts")
        if isinstance(raw, str):
            raw = [raw]
        texts = [str(t) for t in (raw or [])]
        if not texts:
            raise RpcError("texts is required (string or list of strings)", INVALID_PARAMS)
        provider = self._require()._embedder
        vectors = await provider.embed(texts)
        include = bool(params.get("include_vector"))
        items: list[dict[str, Any]] = []
        for vec in vectors:
            norm = sum(x * x for x in vec) ** 0.5
            item: dict[str, Any] = {"norm": round(norm, 6)}
            if include:
                item["embedding"] = vec
            items.append(item)
        return {"model": provider.model_name, "count": len(vectors), "dimension": provider.dimension, "vectors": items}

    async def rerank(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        raw = params.get("documents")
        if isinstance(raw, str):
            raw = [raw]
        documents = [str(d) for d in (raw or [])]
        if not query or not documents:
            raise RpcError("query and documents are required", INVALID_PARAMS)
        model = str(params.get("model") or self._cfg.reranker.model or "")
        if not model:
            raise RpcError("no reranker configured: set reranker.model or pass 'model'", INVALID_PARAMS)
        from ..reranker import rerank as _rerank

        # rerank() sorts the list it is given; carry the caller's index through
        # an owned key so the response can report which document scored where.
        candidates = [{"content": doc, "source": f"doc:{i}"} for i, doc in enumerate(documents)]
        # Blocking (HTTP or local inference) -> worker thread, like core.search's
        # own remote-rerank path.
        ranked = await asyncio.to_thread(
            _rerank,
            query,
            candidates,
            model_name=model,
            top_k=len(documents),
        )
        results = []
        for r in ranked:
            src = str(r.get("source", ""))
            if src.startswith("doc:"):
                idx = int(src[4:])
                results.append({"index": idx, "score": float(r.get("score", 0.0)), "document": documents[idx]})
        return {"model": model, "results": results}

    def watch_subscribe(self, params: dict[str, Any]) -> asyncio.Queue:
        """Return the queue this subscription receives watch events on.

        The transport layer owns the queue's lifetime: it pumps events to the
        socket until the client disconnects, then calls :meth:`unsubscribe`.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # ------------------------------------------------------------------
    # Watcher bridge: background thread -> per-subscriber asyncio queues
    # ------------------------------------------------------------------

    def _on_watch_event(self, event_type: str, summary: str, file_path: Any) -> None:
        self._fanout({"kind": event_type, "summary": summary, "path": str(file_path)})

    def _on_watch_error(self, event_type: str, error: BaseException, file_path: Any) -> None:
        self._fanout({"kind": "error", "event": event_type, "error": str(error), "path": str(file_path)})

    def _fanout(self, payload: dict[str, Any]) -> None:
        # Runs on the watcher thread. The subscriber set is mutated from the
        # event loop, so the snapshot-and-put happens in a threadsafe callback
        # rather than touching queues here.
        if self._loop is None:
            return

        def _dispatch() -> None:
            for q in list(self._subscribers):
                q.put_nowait({"event": "file", "data": payload})

        # The loop can already be closed during daemon shutdown; a watcher-thread
        # event then simply drops instead of raising, so suppress like elsewhere.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(_dispatch)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require(self) -> Any:
        if self._shutting_down:
            raise RpcError("daemon is shutting down", SHUTTING_DOWN)
        if self._ms is None:
            raise RpcError("daemon is not ready", NOT_READY)
        return self._ms

    # Operations a client may invoke. ``start`` is lifecycle-only and stays
    # off this list, as does ``dispatch`` itself and every underscored helper.
    CLIENT_METHODS = frozenset(
        {
            "ping",
            "status",
            "search",
            "index",
            "stats",
            "expand",
            "embed",
            "rerank",
            "watch_subscribe",
            "shutdown",
        }
    )

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Run one client-visible operation by name; await coroutines.

        The transport layer calls this single entry point instead of reflecting
        over method names, and the whitelist keeps lifecycle helpers such as
        ``start`` unreachable from a socket payload.
        """
        if method not in self.CLIENT_METHODS:
            raise RpcError(f"unknown method {method!r}", INTERNAL)
        handler = getattr(self, method)
        result = handler(params if isinstance(params, dict) else {})
        if inspect.isawaitable(result):
            return await result
        return result


def _index_report_dict(paths: list[str], report: Any, *, chunks: int | None) -> dict[str, Any]:
    failures = getattr(report, "failed_files", None) or []
    return {
        "paths": paths,
        "indexed_chunks": chunks if chunks is not None else report.indexed_chunks,
        "indexed_files": report.indexed_files,
        "failed": len(failures),
    }


def _expand_section(chunk: dict[str, Any]) -> dict[str, Any]:
    """Resolve a stored chunk to its full heading-delimited section.

    Mirrors the MCP server's ``memory_expand`` logic (same ``extract_section``
    and ``read_utf8_text_replace``) but returns structured data instead of a
    pre-formatted block, so the transport layer owns presentation.
    """
    from ..expand import extract_section
    from ..io import read_utf8_text_replace

    source = chunk.get("source", "")
    path = Path(source)
    if not path.exists():
        raise RpcError(f"source file not found: {source}", NOT_FOUND)
    all_lines = read_utf8_text_replace(path).splitlines()
    start_line = int(chunk.get("start_line", 1))
    heading_level = int(chunk.get("heading_level", 0) or 0)
    content, first, last = extract_section(all_lines, start_line, heading_level)
    return {
        "source": source,
        "chunk_hash": chunk.get("chunk_hash", ""),
        "first_line": first,
        "last_line": last,
        "heading": chunk.get("heading", ""),
        "content": content,
    }


def _expand_windowed(chunk: dict[str, Any], n: int) -> dict[str, Any]:
    from ..io import read_utf8_text_replace

    source = chunk.get("source", "")
    path = Path(source)
    if not path.exists():
        raise RpcError(f"source file not found: {source}", NOT_FOUND)
    all_lines = read_utf8_text_replace(path).splitlines()
    start_line = int(chunk.get("start_line", 1))
    end_line = int(chunk.get("end_line", start_line))
    ctx_start = max(0, start_line - 1 - n)
    ctx_end = min(len(all_lines), end_line + n)
    return {
        "source": source,
        "chunk_hash": chunk.get("chunk_hash", ""),
        "first_line": ctx_start + 1,
        "last_line": ctx_end,
        "heading": chunk.get("heading", ""),
        "content": "\n".join(all_lines[ctx_start:ctx_end]),
    }


def _non_negative_int(value: Any, *, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RpcError(f"{name} must be an integer", INVALID_PARAMS) from None
    if n < 0:
        raise RpcError(f"{name} must be >= 0", INVALID_PARAMS)
    return n


def _positive_int(value: Any, *, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RpcError(f"{name} must be an integer", INVALID_PARAMS) from None
    if n < 1:
        raise RpcError(f"{name} must be >= 1", INVALID_PARAMS)
    return n
