"""Thin client for the serve daemon — one short-lived connection per call.

The client speaks the same JSON-lines protocol as
:mod:`memsearch.serve.socket_server` but never imports the service: it only
needs :mod:`memsearch.serve.protocol`, so CLI commands and future harnesses
can talk to a running daemon without pulling in the embedding/Milvus stack.

Endpoint resolution, in order:

1. explicit ``socket_path`` / ``tcp_host`` + ``tcp_port`` arguments,
2. ``MEMSEARCH_SERVE_SOCKET`` / ``MEMSEARCH_SERVE_TCP_PORT`` environment
   variables,
3. platform defaults keyed by collection — Unix socket on POSIX, loopback TCP
   on Windows — matching what :meth:`run_daemon` binds when started without
   endpoint options.

Failure to connect raises :class:`DaemonDownError`, which the CLI maps to exit
code 2: issue #780's contract that a dead daemon must look like "service
unavailable", never like an empty result.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from .protocol import (
    DAEMON_DOWN,
    RpcError,
    parse_message,
    parse_response,
    request_line,
)
from .socket_server import DEFAULT_TCP_HOST, DEFAULT_TCP_PORT

ENV_SOCKET = "MEMSEARCH_SERVE_SOCKET"
ENV_TCP_PORT = "MEMSEARCH_SERVE_TCP_PORT"
ENV_NO_DAEMON = "MEMSEARCH_NO_DAEMON"


class DaemonDownError(RuntimeError):
    """No daemon answered at the resolved endpoint."""

    def __init__(self, endpoint: str, cause: Exception | None = None) -> None:
        super().__init__(f"no memsearch serve daemon at {endpoint} "
                         f"(start one with 'memsearch serve start')")
        self.endpoint = endpoint
        self.code = DAEMON_DOWN
        self.__cause__ = cause


class ThinClient:
    """Call a running daemon's methods; see the module docstring for endpoints."""

    def __init__(
        self,
        *,
        collection: str = "default",
        socket_path: str | Path | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
        timeout: float = 30.0,
    ) -> None:
        if socket_path is not None:
            self._kind, self._path, self._host, self._port = "unix", Path(socket_path), None, None
        elif tcp_port is not None or tcp_host is not None:
            self._kind, self._path = "tcp", None
            self._host = tcp_host or DEFAULT_TCP_HOST
            self._port = int(tcp_port or DEFAULT_TCP_PORT)
        else:
            env_socket = os.environ.get(ENV_SOCKET)
            env_port = os.environ.get(ENV_TCP_PORT)
            if env_socket:
                self._kind, self._path, self._host, self._port = "unix", Path(env_socket), None, None
            elif env_port:
                self._kind, self._path = "tcp", None
                self._host = DEFAULT_TCP_HOST
                self._port = int(env_port)
            elif hasattr(asyncio, "start_unix_server"):
                from .pidfile import default_socket_and_pid

                self._kind, self._path = "unix", default_socket_and_pid(collection)[0]
                self._host, self._port = None, None
            else:
                self._kind, self._path = "tcp", None
                self._host, self._port = DEFAULT_TCP_HOST, DEFAULT_TCP_PORT
        self._timeout = float(timeout)

    @property
    def endpoint(self) -> str:
        if self._kind == "unix":
            return str(self._path)
        return f"{self._host}:{self._port}"

    async def probe_alive(self) -> bool:
        """Liveness probe for callers that already run inside an event loop."""
        try:
            await self.call("ping", {}, timeout=3.0)
        except (DaemonDownError, RpcError, OSError, asyncio.TimeoutError):
            return False
        return True

    def is_alive(self) -> bool:
        """Sync liveness probe used by the CLI's automatic routing.

        Async callers (tests, future harnesses) must await probe_alive()
        instead: this method drives its own event loop and would raise inside a
        running one.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            raise RuntimeError("is_alive() is sync-only; await probe_alive() inside an event loop")
        return asyncio.run(self.probe_alive())

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        """Run one method on the daemon and return its ``result``.

        Raises :class:`DaemonDownError` when nothing answers (connection refused,
        socket file missing) and :class:`RpcError` when the daemon reports a
        typed failure.
        """
        try:
            reader, writer = await self._connect()
        except (ConnectionError, FileNotFoundError, OSError) as exc:
            raise DaemonDownError(self.endpoint, exc) from exc
        try:
            writer.write((request_line(1, method, params) + "\n").encode("ascii"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout or self._timeout)
            if not line:
                raise DaemonDownError(self.endpoint)
            response = parse_response(parse_message(line))
        except (ConnectionError, asyncio.CancelledError):
            raise
        except DaemonDownError:
            raise
        except RpcError:
            raise
        except OSError as exc:
            raise DaemonDownError(self.endpoint, exc) from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        if response.error is not None:
            raise RpcError(str(response.error.get("message", "")), str(response.error.get("code", "INTERNAL")))
        return response.result

    def daemon_client_if_alive(self) -> "ThinClient | None":
        """Self-check: return self when the daemon answers, else None."""
        return self if self.is_alive() else None

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._kind == "unix":
            return await asyncio.open_unix_connection(str(self._path))
        return await asyncio.open_connection(self._host, self._port)


def daemon_client_if_alive(collection: str) -> ThinClient | None:
    """Routing helper for CLI commands.

    Returns a live :class:`ThinClient` when automatic routing may proceed:
    ``MEMSEARCH_NO_DAEMON`` is unset and a daemon answers for *collection*.
    Returns ``None`` when the caller should run locally (the pre-daemon
    behaviour, byte-for-byte).
    """
    if os.environ.get(ENV_NO_DAEMON):
        return None
    client = ThinClient(collection=collection)
    return client.daemon_client_if_alive()
