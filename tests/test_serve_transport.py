"""Transport tests: JSON-lines socket, HTTP adapter, and the thin client.

Everything runs against real loopback sockets but a fake service — no Milvus,
no model — so framing, dispatch, error mapping, event streaming and the
daemon-down contract are exercised cheaply. The fake mirrors the small surface
the transports rely on (``dispatch``, ``watch_subscribe``, ``unsubscribe``).
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.request

import pytest

from memsearch.serve.client import ENV_NO_DAEMON, DaemonDownError, ThinClient, daemon_client_if_alive
from memsearch.serve.http_server import HttpServer
from memsearch.serve.protocol import INTERNAL, INVALID_PARAMS, NOT_FOUND, RpcError, request_line
from memsearch.serve.socket_server import SocketServer


class FakeService:
    CLIENT_METHODS = frozenset({"ping", "search", "watch_subscribe", "shutdown"})

    def __init__(self) -> None:
        self.stopped = False
        self.subs: set[asyncio.Queue] = set()

    async def dispatch(self, method, params):
        if method == "ping":
            return {"pong": True}
        if method == "search":
            if not params.get("query"):
                raise RpcError("query is required", INVALID_PARAMS)
            return {"results": [{"score": 1.0, "source": "s.md", "content": params["query"]}]}
        if method == "shutdown":
            self.stopped = True
            return {}
        if method == "expand":
            raise RpcError("no such chunk", NOT_FOUND)
        if method == "stats":
            raise RuntimeError("kaput")
        raise RpcError(f"unknown method {method!r}", INTERNAL)

    def watch_subscribe(self, params) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        self.subs.discard(q)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_thin_client_round_trip_over_tcp() -> None:
    srv = SocketServer(FakeService(), tcp_port=0)
    await srv.start()
    try:
        client = ThinClient(tcp_port=srv.tcp_port)
        assert await client.call("ping", {}) == {"pong": True}
        data = await client.call("search", {"query": "hello 中文"})
        assert data["results"][0]["content"] == "hello 中文"
        assert await client.probe_alive() is True
    finally:
        await srv.close()


async def test_thin_client_maps_rpc_errors() -> None:
    srv = SocketServer(FakeService(), tcp_port=0)
    await srv.start()
    try:
        client = ThinClient(tcp_port=srv.tcp_port)
        with pytest.raises(RpcError) as exc:
            await client.call("expand", {})
        assert exc.value.code == NOT_FOUND
    finally:
        await srv.close()


async def test_unexpected_exception_becomes_internal_error() -> None:
    srv = SocketServer(FakeService(), tcp_port=0)
    await srv.start()
    try:
        client = ThinClient(tcp_port=srv.tcp_port)
        with pytest.raises(RpcError) as exc:
            await client.call("stats", {})
        assert exc.value.code == INTERNAL
        assert "kaput" in exc.value.message
    finally:
        await srv.close()


async def test_malformed_line_gets_an_error_envelope_and_connection_survives() -> None:
    srv = SocketServer(FakeService(), tcp_port=0)
    await srv.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.tcp_port)
        writer.write(b"not json\n")
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert reply["error"]["code"] == INVALID_PARAMS
        # The connection stays usable after a bad line.
        writer.write(request_line(2, "ping", {}).encode("ascii") + b"\n")
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 5))["result"] == {"pong": True}
        writer.close()
    finally:
        await srv.close()


async def test_watch_subscribe_streams_events_on_the_same_connection() -> None:
    service = FakeService()
    srv = SocketServer(service, tcp_port=0)
    await srv.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.tcp_port)
        writer.write(request_line(7, "watch_subscribe", {}).encode("ascii") + b"\n")
        await writer.drain()
        ack = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert ack == {"id": 7, "result": {"subscribed": True}}
        queue = next(iter(service.subs))
        queue.put_nowait({"event": "file", "data": {"kind": "modified", "path": "a.md"}})
        event = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert event["subscription"] == 7
        assert event["data"] == {"kind": "modified", "path": "a.md"}
        writer.close()
    finally:
        await srv.close()


async def test_daemon_down_raises_instead_of_returning_empty() -> None:
    client = ThinClient(tcp_port=_free_port())
    with pytest.raises(DaemonDownError):
        await client.call("ping", {})
    assert await client.probe_alive() is False


def test_env_no_daemon_disables_routing(monkeypatch) -> None:
    monkeypatch.setenv(ENV_NO_DAEMON, "1")
    assert daemon_client_if_alive("anything") is None


async def test_http_rpc_and_health() -> None:
    http = HttpServer(FakeService(), loop=asyncio.get_running_loop(), port=0)
    http.start()
    try:

        def post(payload: dict) -> dict:
            req = urllib.request.Request(
                http.endpoint + "/rpc",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)

        def get(path: str) -> dict:
            with urllib.request.urlopen(http.endpoint + path, timeout=10) as resp:
                return json.load(resp)

        reply = await asyncio.to_thread(post, {"id": 1, "method": "ping", "params": {}})
        assert reply == {"id": 1, "result": {"pong": True}}
        health = await asyncio.to_thread(get, "/health")
        assert health["ok"] is True
    finally:
        http.stop()
