"""Framework-backed MCP server backends: standalone ``fastmcp`` and the official SDK.

Both frameworks implement the same idea — declare a tool as a Python function
and the framework derives the JSON schema from the signature and docstring,
validates the arguments, and owns the protocol plumbing (handshake,
``tools/list``, ``tools/call``, error envelopes, transports).

This module keeps **one** tool surface and plugs either framework underneath
it, so the two backends cannot drift apart:

``fastmcp``
    ``pip install 'memsearch[fastmcp]'`` → ``from fastmcp import FastMCP``
    (the standalone project; richer: in-memory client, middleware, auth, proxy).

``official``
    ``pip install 'memsearch[mcp-official]'`` → ``mcp.server.mcpserver.MCPServer``
    (the first-party SDK; in 2.x the high-level server was renamed from
    ``FastMCP`` to ``MCPServer``).

The stdlib server in :mod:`memsearch.mcp_server` stays the default: it needs no
third-party dependency, so it remains the variant that can be patched into an
installed copy of the package.

The tool implementations themselves are not duplicated here — they are the
``MCPServer.tool_*`` methods of the stdlib server, so both backends return
byte-identical results.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from .config import MemSearchConfig
from .mcp_server import (
    DEFAULT_KNOWLEDGE_DB,
    KNOWLEDGE_DB_ENV,
    SERVER_NAME,
    SERVER_VERSION,
    MCPServer,
    _reconfigure_streams,
)

#: Backends this module can drive. ``stdlib`` lives in :mod:`memsearch.mcp_server`.
FRAMEWORKS = ("fastmcp", "official")

#: Canonical tool annotations (wire format, camelCase per the MCP spec).
#: The official SDK wants model instances instead, so they are converted below.
ANNOTATIONS_READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
ANNOTATIONS_READ_LOCAL = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
ANNOTATIONS_UPSERT = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: MCP spec field name -> official SDK model field name.
_OFFICIAL_ANNOTATION_FIELDS = {
    "readOnlyHint": "read_only_hint",
    "destructiveHint": "destructive_hint",
    "idempotentHint": "idempotent_hint",
    "openWorldHint": "open_world_hint",
}


def _load(framework: str) -> tuple[Any, Any]:
    """Return ``(server_class, tool_error_class)`` for *framework*."""
    if framework == "fastmcp":
        try:
            from fastmcp import FastMCP
            from fastmcp.exceptions import ToolError
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise SystemExit(
                "the fastmcp backend needs the optional dependency:\n"
                "    pip install 'memsearch[fastmcp]'\n"
                "or run it without installing: "
                "uvx --with fastmcp --from memsearch memsearch mcp --backend fastmcp"
            ) from exc
        return FastMCP, ToolError
    if framework == "official":
        try:
            from mcp.server.mcpserver import MCPServer as OfficialServer
            from mcp.server.mcpserver.exceptions import ToolError
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise SystemExit(
                "the official backend needs the optional dependency:\n"
                "    pip install 'memsearch[mcp-official]'\n"
                "or run it without installing: "
                "uvx --with mcp --from memsearch memsearch mcp --backend official"
            ) from exc
        return OfficialServer, ToolError
    raise SystemExit(f"unknown backend {framework!r} (choose one of {', '.join(FRAMEWORKS)})")


def _annotations(framework: str, payload: dict[str, Any]) -> Any:
    """Adapt the canonical annotation dict to what *framework* expects."""
    if framework == "fastmcp":
        return dict(payload)
    from mcp.types import ToolAnnotations

    fields = {
        _OFFICIAL_ANNOTATION_FIELDS[key]: value for key, value in payload.items() if key in _OFFICIAL_ANNOTATION_FIELDS
    }
    return ToolAnnotations(**fields)


def build_server(
    *,
    framework: str,
    config: MemSearchConfig,
    collection: str | None = None,
    knowledge_db: str | None = None,
    max_content_chars: int = 2000,
    backend: MCPServer | None = None,
):
    """Return a configured MCP server object for *framework*.

    The returned object can be run (``.run()``) or driven in-process by the
    framework's own test client. Pass *backend* to reuse an already configured
    :class:`~memsearch.mcp_server.MCPServer` (:func:`serve_backends` does this so
    it can close the lazily created store on shutdown).
    """
    server_cls, tool_error_cls = _load(framework)

    backend = backend or MCPServer(
        config=config,
        collection=collection,
        knowledge_db=knowledge_db,
        max_content_chars=max_content_chars,
    )

    def call(handler, **arguments: Any) -> str:
        """Run one backend handler, mapping failures to clean MCP tool errors."""
        try:
            return handler(arguments)
        except ValueError as exc:
            raise tool_error_cls(str(exc)) from exc
        except Exception as exc:  # report the real failure to the client
            raise tool_error_cls(f"{type(exc).__name__}: {exc}") from exc

    server = server_cls(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "memsearch MCP: memory_search recalls past sessions, memory_expand "
            "returns full context, embed/rerank call the configured backends."
        ),
    )

    @server.tool(annotations=_annotations(framework, ANNOTATIONS_READ))
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

    @server.tool(annotations=_annotations(framework, ANNOTATIONS_UPSERT))
    def memory_index(paths: list[str], force: bool = False) -> str:
        """Index markdown files or directories into the memsearch collection.

        Set force=true to re-embed everything.
        """
        return call(backend.tool_memory_index, paths=paths, force=force)

    @server.tool(annotations=_annotations(framework, ANNOTATIONS_READ_LOCAL))
    def memory_stats() -> str:
        """Show the chunk count and indexed source files of a collection."""
        return call(backend.tool_memory_stats)

    @server.tool(annotations=_annotations(framework, ANNOTATIONS_READ_LOCAL))
    def memory_expand(chunk_id: str, lines: int | None = None) -> str:
        """Expand one memory chunk to its full heading section.

        Progressive disclosure step 2: search -> expand -> transcript.
        chunk_id is the chunk_hash returned by memory_search; lines shows N
        lines around the chunk instead of the whole heading section.
        """
        return call(backend.tool_memory_expand, chunk_id=chunk_id, lines=lines)

    @server.tool(annotations=_annotations(framework, ANNOTATIONS_READ))
    def embed(texts: list[str], include_vector: bool = False) -> str:
        """Embed text with the configured embedding provider (OpenAI-compatible by default).

        include_vector=true returns the full vectors (large).
        """
        return call(backend.tool_embed, texts=texts, include_vector=include_vector)

    @server.tool(annotations=_annotations(framework, ANNOTATIONS_READ))
    def rerank(query: str, documents: list[str]) -> str:
        """Cross-encoder rerank documents against a query with the configured remote reranker.

        Requires reranker.model = 'remote:<model>' in ~/.memsearch/config.toml.
        """
        return call(backend.tool_rerank, query=query, documents=documents)

    # The knowledge tool is advertised only when the third-party dsh-knowledge
    # store exists (same gate as the stdlib backend's tool_definitions()).
    if backend.knowledge_db and Path(backend.knowledge_db).expanduser().exists():

        @server.tool(annotations=_annotations(framework, ANNOTATIONS_READ))
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

    return server


def serve_backends(
    *,
    framework: str,
    config: MemSearchConfig,
    collection: str | None = None,
    knowledge_db: str | None = None,
    max_content_chars: int = 2000,
) -> None:
    """Run a framework-backed MCP server over stdio until the client disconnects."""
    _reconfigure_streams()
    backend = MCPServer(
        config=config,
        collection=collection,
        knowledge_db=knowledge_db,
        max_content_chars=max_content_chars,
    )
    server = build_server(
        framework=framework,
        config=config,
        collection=collection,
        knowledge_db=knowledge_db,
        max_content_chars=max_content_chars,
        backend=backend,
    )
    try:
        # The standalone fastmcp prints a banner unless told not to; the official
        # SDK has no such option.
        kwargs: dict[str, Any] = {"transport": "stdio"}
        if "show_banner" in inspect.signature(server.run).parameters:
            kwargs["show_banner"] = False
        server.run(**kwargs)
    finally:
        backend.close()


__all__ = [
    "ANNOTATIONS_READ",
    "ANNOTATIONS_READ_LOCAL",
    "ANNOTATIONS_UPSERT",
    "DEFAULT_KNOWLEDGE_DB",
    "FRAMEWORKS",
    "KNOWLEDGE_DB_ENV",
    "build_server",
    "serve_backends",
]
