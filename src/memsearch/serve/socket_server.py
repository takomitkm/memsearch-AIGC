"""JSON-lines transports for the serve daemon: TCP and Unix-domain sockets.

One ``asyncio.start_server`` per daemon; every connection runs in its own task
and shares the single :class:`~memsearch.serve.service.ServeService`. The
transport only moves bytes: each inbound line is parsed by
:mod:`memsearch.serve.protocol`, dispatched through ``ServeService.dispatch``,
and the reply serialized back as one line.

A ``watch_subscribe`` request switches the *current connection* into an event
stream: the immediate reply confirms the subscription, then watcher events are
pushed as ``event`` lines until the client hangs up. A client that wants both
events and request/response traffic opens two connections — keeping one
connection single-mode avoids interleaving replies and events from two tasks.

Windows has no ``asyncio.start_unix_server``, so daemons there (and anywhere
``--tcp-port`` is given) listen on loopback TCP instead; the default port is
shared with :mod:`memsearch.serve.client` so the thin client finds it without
configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from .protocol import (
    INTERNAL,
    INVALID_PARAMS,
    event_line,
    error_line,
    parse_message,
    parse_request,
    response_line,
)

#: Loopback port used when no Unix socket is available (Windows) or ``--tcp-port``
#: is not given. Matches the thin client's default so discovery needs no config.
def _wire(text: str) -> bytes:
    """Frame one protocol line: newline-terminated ASCII (protocol escapes non-ASCII)."""
    return (text + "\n").encode("ascii")


DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 19834


class SocketServer:
    """Serve one :class:`ServeService` over a TCP or Unix-domain JSON-lines socket."""

    def __init__(
        self,
        service: Any,
        *,
        unix_path: str | Path | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        if unix_path is not None:
            self._path = Path(unix_path)
            self._host: str | None = None
            self._port: int | None = None
        else:
            self._path = None
            self._host = tcp_host or DEFAULT_TCP_HOST
            # Port 0 means "pick an ephemeral port" for tests and ad-hoc use;
            # only None falls back to the shared default the client probes.
            self._port = int(tcp_port) if tcp_port is not None else DEFAULT_TCP_PORT
        self._service = service
        self._shutdown_event = shutdown_event
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Bind the socket. Raises ``OSError`` when the port/path is taken."""
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._server = await asyncio.start_unix_server(self._handle, path=str(self._path))
        else:
            self._server = await asyncio.start_server(self._handle, host=self._host, port=self._port)

    @property
    def endpoint(self) -> str:
        """Human-readable endpoint for logs and ``serve status``."""
        if self._path is not None:
            return str(self._path)
        return f"{self._host}:{self._port}"

    @property
    def tcp_port(self) -> int | None:
        """The bound TCP port (useful when started with port 0)."""
        if self._server is not None and self._port is not None:
            sockets = getattr(self._server, "sockets", None) or []
            for s in sockets:
                return s.getsockname()[1]
        return self._port

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("SocketServer.start() was not called")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        # gather consumes the cancellations without a try/except per task
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        if self._path is not None:
            # A Unix socket file outlives the listener; a stale one would make
            # the next bind fail with EADDRINUSE.
            with contextlib.suppress(FileNotFoundError):
                self._path.unlink()

    # ------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        subscribed_queue: asyncio.Queue | None = None
        subscription_id: Any = None
        try:
            while True:
                if subscribed_queue is not None:
                    # Event mode: wait for the next watcher event *or* the client
                    # hanging up, so a quiet subscription cannot leak the task.
                    getter = asyncio.ensure_future(subscribed_queue.get())
                    reader_task = asyncio.ensure_future(reader.readline())
                    done, pending = await asyncio.wait(
                        {getter, reader_task}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    for pending_task in pending:
                        pending_task.cancel()
                    # Consume the cancellations: an un-awaited cancelled task
                    # logs "Task was destroyed but it is pending" at GC time.
                    await asyncio.gather(*pending, return_exceptions=True)
                    if reader_task in done:
                        # EOF or a stray line in event mode: the client is gone
                        # or using the connection unsupportedly — end the stream.
                        break
                    event = getter.result()
                    writer.write(_wire(event_line(subscription_id, "file", event.get("data", {}))))
                    await writer.drain()
                    continue
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = parse_request(parse_message(line))
                except Exception as exc:  # malformed inbound line
                    writer.write(_wire(error_line(None, INVALID_PARAMS, f"malformed request: {exc}")))
                    await writer.drain()
                    continue
                if req.method == "watch_subscribe":
                    subscribed_queue = self._service.watch_subscribe(req.params or {})
                    subscription_id = req.id
                    writer.write(_wire(response_line(req.id, {"subscribed": True})))
                    await writer.drain()
                    continue
                try:
                    result = await self._service.dispatch(req.method, req.params)
                    writer.write(_wire(response_line(req.id, result)))
                    if req.method == "shutdown" and self._shutdown_event is not None:
                        self._shutdown_event.set()
                except Exception as exc:
                    writer.write(_wire(self._error_for(req.id, exc)))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if subscribed_queue is not None:
                unsubscribe = getattr(self._service, "unsubscribe", None)
                if callable(unsubscribe):
                    unsubscribe(subscribed_queue)
            writer.close()
            with contextlib.suppress(ConnectionError, asyncio.CancelledError):
                await writer.wait_closed()
            if task is not None:
                self._tasks.discard(task)

    @staticmethod
    def _error_for(req_id: Any, exc: Exception) -> str:
        from .protocol import RpcError

        if isinstance(exc, RpcError):
            return error_line(req_id, exc.code, exc.message)
        return error_line(req_id, INTERNAL, f"{type(exc).__name__}: {exc}")
