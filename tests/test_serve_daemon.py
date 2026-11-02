"""Daemon lifecycle end-to-end with a fake service.

Exercises the real control flow of :func:`run_daemon` — pidfile claim, socket
bind, dispatch, client-triggered shutdown, teardown — while the service itself
is a stub, so no Milvus store or embedding provider is created.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from memsearch.serve.client import ThinClient
from memsearch.serve.daemon import run_daemon
from memsearch.serve.pidfile import DaemonAlreadyRunningError, acquire
from memsearch.serve.protocol import INTERNAL, RpcError


class FakeService:
    CLIENT_METHODS = frozenset({"ping", "status", "shutdown"})

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def shutdown(self, params=None) -> None:
        self.stopped = True

    @property
    def is_ready(self) -> bool:
        return self.started and not self.stopped

    async def dispatch(self, method, params):
        if method == "ping":
            return {"pong": True}
        if method == "status":
            return {"ready": self.is_ready, "collection": "test"}
        if method == "shutdown":
            return {}
        raise RpcError(f"unknown method {method!r}", INTERNAL)

    def watch_subscribe(self, params) -> asyncio.Queue:
        return asyncio.Queue()

    def unsubscribe(self, q) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_daemon_serves_until_a_client_shuts_it_down(tmp_path) -> None:
    service = FakeService()
    pidfile = tmp_path / "serve.pid"
    port = _free_port()
    task = asyncio.create_task(
        run_daemon(
            None,
            [str(tmp_path)],
            tcp_port=port,
            pidfile_path=pidfile,
            service=service,
            install_signals=False,
        )
    )
    client = ThinClient(tcp_port=port)
    try:
        for _ in range(100):  # up to ~5 s for the bind + first accept
            try:
                await client.call("ping", {}, timeout=1.0)
                break
            except Exception:
                await asyncio.sleep(0.05)
        assert pidfile.exists(), "pidfile must be claimed while the daemon runs"
        status = await client.call("status", {})
        assert status["ready"] is True
        await client.call("shutdown", {})
        assert await asyncio.wait_for(task, 10) == 0
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await task
    assert service.started is True
    assert service.stopped is True
    assert not pidfile.exists(), "teardown must release the pidfile"


async def test_second_daemon_is_refused_by_the_pidfile(tmp_path) -> None:
    pidfile = tmp_path / "serve.pid"
    handle = acquire(pidfile)  # this (live) test process owns it
    try:
        with pytest.raises(DaemonAlreadyRunningError):
            await run_daemon(
                None,
                [str(tmp_path)],
                tcp_port=_free_port(),
                pidfile_path=pidfile,
                service=FakeService(),
                install_signals=False,
            )
    finally:
        handle.release()
