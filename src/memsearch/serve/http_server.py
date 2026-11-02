"""Optional HTTP transport for the serve daemon: ``POST /rpc`` + ``GET /health``.

For clients that are not Python and cannot speak the JSON-lines socket
protocol (issue #780 names Hermes). Built on stdlib ``ThreadingHTTPServer``:
its handler threads hand each request to the daemon's asyncio loop via
``run_coroutine_threadsafe`` and block on the future, so the single
:class:`ServeService` stays the one owner of the Milvus store regardless of
transport. No new dependency, matching the rest of ``serve/``.

Endpoints
---------
``POST /rpc``   body ``{"id": ..., "method": ..., "params": {...}}`` → the same
                ``{"id", "result"|"error"}`` envelope the socket speaks.
``GET /health`` → ``{"ok": true, "ready": bool}`` for liveness probes.
Anything else   → 404.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .protocol import INTERNAL, INVALID_PARAMS, RpcError


class HttpServer:
    """Expose one :class:`ServeService` over HTTP on a background thread."""

    def __init__(self, service: Any, *, loop: asyncio.AbstractEventLoop, host: str = "127.0.0.1", port: int = 19835) -> None:
        self._service = service
        self._loop = loop
        self._host = host
        self._port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        service = self._service
        loop = self._loop

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
                method = body.get("method")
                if not isinstance(method, str):
                    return {"id": body.get("id"), "error": {"code": INVALID_PARAMS, "message": "missing 'method'"}}
                coro = service.dispatch(method, body.get("params") or {})
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                try:
                    result = fut.result(timeout=300)
                except RpcError as exc:
                    return {"id": body.get("id"), "error": {"code": exc.code, "message": exc.message}}
                except Exception as exc:  # noqa: BLE001 - surface as a typed error envelope
                    return {"id": body.get("id"), "error": {"code": INTERNAL, "message": f"{type(exc).__name__}: {exc}"}}
                return {"id": body.get("id"), "result": result}

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                if self.path != "/rpc":
                    self._send(404, {"error": {"code": "NOT_FOUND", "message": f"unknown path {self.path}"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("body must be a JSON object")
                except (ValueError, json.JSONDecodeError) as exc:
                    self._send(400, {"error": {"code": INVALID_PARAMS, "message": f"invalid JSON body: {exc}"}})
                    return
                self._send(200, self._dispatch(body))

            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                if self.path == "/health":
                    self._send(200, {"ok": True, "ready": bool(getattr(service, "is_ready", False))})
                    return
                self._send(404, {"error": {"code": "NOT_FOUND", "message": f"unknown path {self.path}"}})

            def log_message(self, *args: Any) -> None:  # keep daemon logs quiet
                pass

        self._httpd = ThreadingHTTPServer((self._host, self._port), Handler)
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="memsearch-serve-http", daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://{self._host}:{self._port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
