"""The ``memsearch serve`` daemon: lifecycle, endpoints, graceful shutdown.

Brings together the pieces: claims the pidfile (exactly one daemon per
collection), starts the :class:`ServeService` (store + initial index +
watcher), binds the JSON-lines socket and optionally the HTTP adapter, then
serves until one of:

* a client calls ``shutdown`` (the socket server sets the shared event),
* SIGINT/SIGTERM arrives (signal handlers where the platform supports them,
  ``KeyboardInterrupt`` where it does not — Windows),
* :meth:`SocketServer.serve_forever` dies on its own (bind failure of the
  listening socket after startup, ...).

Every exit path funnels into the same teardown: stop servers → stop watcher →
close the Milvus store (releasing the file lock) → release the pidfile. The
returned integer is the process exit code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Any

from .http_server import HttpServer
from .pidfile import acquire
from .service import ServeService
from .socket_server import DEFAULT_TCP_PORT, SocketServer

log = logging.getLogger("memsearch.serve")


async def run_daemon(
    cfg: Any,
    paths: list[str],
    *,
    socket_path: str | Path | None = None,
    tcp_port: int | None = None,
    http_port: int | None = None,
    pidfile_path: str | Path | None = None,
    service: ServeService | None = None,
    install_signals: bool = True,
) -> int:
    """Run the daemon until shutdown; returns the process exit code.

    Endpoint selection: an explicit ``socket_path`` (Unix) or ``tcp_port`` wins;
    with neither given, Unix socket is the default on POSIX and loopback TCP on
    Windows — the same defaults the thin client resolves, so no configuration
    is needed to connect the two. Pass *service* to inject a pre-built
    ``ServeService`` (tests); otherwise one is constructed from *cfg*/*paths*.
    """
    collection = getattr(getattr(cfg, "milvus", None), "collection", "default")
    if socket_path is None and tcp_port is None and http_port is None:
        if hasattr(asyncio, "start_unix_server"):
            from .pidfile import default_socket_and_pid

            socket_path = default_socket_and_pid(collection)[0]
        else:
            tcp_port = DEFAULT_TCP_PORT
    pid_path = Path(pidfile_path) if pidfile_path else _default_pidfile(collection)

    handle = acquire(Path(pid_path))
    svc = service if service is not None else ServeService(cfg, paths)
    socket_srv: SocketServer | None = None
    http_srv: HttpServer | None = None
    signal_sources: list[Any] = []
    try:
        await svc.start()
        loop = asyncio.get_running_loop()
        socket_srv = SocketServer(
            svc,
            unix_path=socket_path,
            tcp_port=tcp_port,
            shutdown_event=loop.create_event(),
        )
        await socket_srv.start()
        if http_port is not None:
            http_srv = HttpServer(svc, loop=loop, port=http_port)
            http_srv.start()
        log.info("memsearch serve ready: %s%s (pidfile %s)", socket_srv.endpoint,
                 f" + {http_srv.endpoint}" if http_srv else "", pid_path)

        if install_signals and hasattr(loop, "add_signal_handler"):
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(ValueError, OSError, RuntimeError):
                    loop.add_signal_handler(sig, socket_srv._shutdown_event.set)  # noqa: SLF001
                    signal_sources.append(sig)

        serve_task = asyncio.create_task(socket_srv.serve_forever(), name="serve-socket")
        stop_event: asyncio.Event = socket_srv._shutdown_event  # noqa: SLF001 - same module
        stop_task = asyncio.create_task(stop_event.wait(), name="serve-stop")
        done, _pending = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                log.error("serve loop failed: %s", exc)
                return 1
        return 0
    finally:
        for sig in signal_sources:
            with contextlib.suppress(ValueError, OSError, RuntimeError):
                loop.remove_signal_handler(sig)
        if socket_srv is not None:
            await socket_srv.close()
        if http_srv is not None:
            http_srv.stop()
        await svc.shutdown()
        handle.release()
        log.info("memsearch serve stopped")


def _default_pidfile(collection: str) -> Path:
    from .pidfile import default_socket_and_pid

    return default_socket_and_pid(collection)[1]


