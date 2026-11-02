"""FastMCP backend for ``memsearch mcp`` — optional, third-party ``fastmcp``.

The default MCP server (:mod:`memsearch.mcp_server`) is hand-rolled on the
standard library: no third-party dependency, and small enough to patch into an
installed package.  This backend serves the *same* tools through the standalone
`fastmcp <https://github.com/jlowin/fastmcp>`_ project instead, which derives
each tool's input schema from the function signature and docstring, validates
arguments, and owns the protocol/transport plumbing.

Pick this backend when the deployment already has fastmcp, or when you want
what it adds (HTTP transports, auth providers, middleware, composition).  The
tool implementations themselves are shared with the stdlib server: this module
wraps one :class:`memsearch.mcp_server.MCPServer` instead of reimplementing
search/index/expand/embed/rerank.

Usage::

    pip install 'memsearch[fastmcp]'
    memsearch mcp --backend fastmcp
"""

from __future__ import annotations

from typing import Any

from .config import MemSearchConfig
from .mcp_server import (
    ANNOTATIONS_READ,
    ANNOTATIONS_READ_LOCAL,
    ANNOTATIONS_UPSERT,
    DEFAULT_KNOWLEDGE_DB,
    KNOWLEDGE_DB_ENV,
    SERVER_NAME,
    SERVER_VERSION,
    MCPServer,
    _reconfigure_streams,
)


def _load_fastmcp():
    """Import fastmcp lazily so the stdlib backend never needs it."""
    try:
        from fastmcp import FastMCP
        from fastmcp.exceptions import ToolError
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit(
            "the fastmcp backend requires the optional dependency:\n"
            "    pip install 'memsearch[fastmcp]'\n"
            "or run it without installing:  uvx --with fastmcp --from memsearch memsearch mcp --backend fastmcp"
        ) from exc
    return FastMCP, ToolError


def build_server(
    *,
    config: MemSearchConfig,
    collection: str | None = None,
    knowledge_db: str | None = None,
    max_content_chars: int = 2000,
    backend: MCPServer | None = None,
    daemon: bool = False,
):
    """Return a configured ``fastmcp.FastMCP`` instance exposing memsearch tools.

    The returned object can be run (``mcp.run()``) or passed to
    ``fastmcp.Client`` for in-process tests.  Pass *backend* to reuse an
    already configured :class:`MCPServer` (``serve_fastmcp`` does this so it
    can close the lazily created store on shutdown).
    """
    FastMCP, ToolError = _load_fastmcp()

    backend = backend or MCPServer(
        config=config,
        collection=collection,
        knowledge_db=knowledge_db,
        max_content_chars=max_content_chars,
        daemon=daemon,
    )

    def call(handler, **arguments: Any) -> str:
        """Run one backend handler, mapping failures to clean MCP tool errors."""
        try:
            return handler(arguments)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    mcp = FastMCP(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "memsearch MCP: memory_search recalls past sessions, memory_expand "
            "returns full context, embed/rerank call the configured backends."
        ),
    )

    # MCP tool annotations (mcp-builder best practices): read-only tools never
    # mutate the store; remote embedding/rerank calls reach the outside world;
    # indexing is an idempotent upsert, not a destructive operation.

    @mcp.tool(annotations=ANNOTATIONS_READ)
    def memory_search(query: str, limit: int = 5, source_prefix: str | None = None) -> str:
        """Search memsearch's persistent memory (hybrid dense + BM25, optionally reranked).

        Use it to recall earlier decisions, commands, files and outcomes.
        """
        return call(
            backend.tool_memory_search,
            query=query,
            limit=limit,
            source_prefix=source_prefix,
        )

    @mcp.tool(annotations=ANNOTATIONS_UPSERT)
    def memory_index(paths: list[str], force: bool = False) -> str:
        """Index markdown files or directories into the memsearch collection.

        Set force=true to re-embed everything.
        """
        return call(backend.tool_memory_index, paths=paths, force=force)

    @mcp.tool(annotations=ANNOTATIONS_READ_LOCAL)
    def memory_stats() -> str:
        """Show the chunk count and indexed source files of a collection."""
        return call(backend.tool_memory_stats)

    @mcp.tool(annotations=ANNOTATIONS_READ_LOCAL)
    def memory_expand(chunk_id: str, lines: int | None = None) -> str:
        """Expand one memory chunk to its full heading section.

        Progressive disclosure step 2: search -> expand -> transcript.
        chunk_id is the chunk_hash returned by memory_search; lines shows N
        lines around the chunk instead of the whole heading section.
        """
        return call(backend.tool_memory_expand, chunk_id=chunk_id, lines=lines)

    @mcp.tool(annotations=ANNOTATIONS_READ)
    def embed(texts: list[str], include_vector: bool = False) -> str:
        """Embed text with the configured embedding provider (OpenAI-compatible by default).

        include_vector=true returns the full vectors (large).
        """
        return call(backend.tool_embed, texts=texts, include_vector=include_vector)

    @mcp.tool(annotations=ANNOTATIONS_READ)
    def rerank(query: str, documents: list[str]) -> str:
        """Cross-encoder rerank documents against a query with the configured remote reranker.

        Requires reranker.model = 'remote:<model>' in ~/.memsearch/config.toml.
        """
        return call(backend.tool_rerank, query=query, documents=documents)

    # The knowledge tool is advertised only when the third-party dsh-knowledge
    # store exists (same gate as the stdlib backend's tool_definitions()).
    if backend.has_knowledge_store():

        @mcp.tool(annotations=ANNOTATIONS_READ)
        def knowledge_search(query: str, limit: int = 5, base_id: str | None = None) -> str:
            """Hybrid search over the knowledge store of the third-party dsh-knowledge DSH plugin.

            Dense vectors + BM25 with cross-encoder reranking. base_id restricts
            the search to one knowledge base.
            """
            return call(
                backend.tool_knowledge_search,
                query=query,
                limit=limit,
                base_id=base_id,
            )

    return mcp


def serve_fastmcp(
    *,
    config: MemSearchConfig,
    collection: str | None = None,
    knowledge_db: str | None = None,
    max_content_chars: int = 2000,
    daemon: bool = False,
) -> None:
    """Run the FastMCP-backed server over stdio until the client disconnects."""
    _reconfigure_streams()
    backend = MCPServer(
        config=config,
        collection=collection,
        knowledge_db=knowledge_db,
        max_content_chars=max_content_chars,
        daemon=daemon,
    )
    mcp = build_server(
        config=config,
        collection=collection,
        knowledge_db=knowledge_db,
        max_content_chars=max_content_chars,
        backend=backend,
    )
    try:
        # No banner: stdio servers are embedded, and stderr should stay quiet.
        mcp.run(transport="stdio", show_banner=False)
    finally:
        backend.close()


__all__ = ["DEFAULT_KNOWLEDGE_DB", "KNOWLEDGE_DB_ENV", "build_server", "serve_fastmcp"]
