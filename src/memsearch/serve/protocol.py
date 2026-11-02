"""Wire format for the serve daemon: JSON-lines requests, responses, events.

One JSON object per line. This module owns the message *shape* and the shared
vocabulary (method names, error codes) so the thin client and the server's
transport adapters agree without either importing the other. It carries no
business logic and imports nothing from ``core``/``store``/``service``.

Framing safety: ``ensure_ascii=True`` escapes everything above ASCII and every
control character, so a serialized message never contains a raw newline and the
line split is unambiguous even for CJK or emoji payloads. That also sidesteps
lone-surrogate crashes on encode that ``ensure_ascii=False`` would need a
sanitizer to avoid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# The methods a live daemon answers. Kept here as the single source of truth:
# the client validates against it, the server dispatches against the same names.
METHODS = frozenset(
    {
        "ping",
        "status",
        "search",
        "knowledge_search",
        "index",
        "stats",
        "expand",
        "embed",
        "rerank",
        "watch_subscribe",
        "shutdown",
    }
)

# Error codes. DAEMON_DOWN is never sent on the wire — it is what the thin client
# raises locally when no daemon answers, the #780 "service down vs empty result"
# distinction. The rest are produced by the server.
INVALID_PARAMS = "INVALID_PARAMS"
NOT_FOUND = "NOT_FOUND"
NOT_READY = "NOT_READY"
INDEX_FAILED = "INDEX_FAILED"
UNKNOWN_METHOD = "UNKNOWN_METHOD"
SHUTTING_DOWN = "SHUTTING_DOWN"
INTERNAL = "INTERNAL"
DAEMON_DOWN = "DAEMON_DOWN"


class ServeProtocolError(ValueError):
    """A received line is not a well-formed protocol message."""


class RpcError(Exception):
    """A typed failure from a service operation.

    Service code raises this instead of building envelopes by hand; the server's
    transport layer converts it to an error response carrying the same code and
    message. ``code`` defaults to ``INTERNAL`` so an unexpected exception mapped
    through this type still yields a well-formed error rather than a crash.
    """

    def __init__(self, message: str, code: str = INTERNAL) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Request:
    """A parsed client request."""

    id: Any
    method: str
    params: dict[str, Any]


@dataclass
class Response:
    """A parsed server response — either a result or an error, never both."""

    id: Any
    result: Any | None
    error: dict[str, str] | None

    @property
    def ok(self) -> bool:
        return self.error is None


def encode_message(obj: dict[str, Any]) -> str:
    """Serialize one message to a single newline-free line."""
    return json.dumps(obj, ensure_ascii=True)


def request_line(req_id: Any, method: str, params: dict[str, Any] | None = None) -> str:
    """Build a request line. Validates against the shared method vocabulary."""
    if method not in METHODS:
        raise ServeProtocolError(f"unknown method {method!r}")
    return encode_message({"id": req_id, "method": method, "params": params or {}})


def response_line(req_id: Any, result: Any) -> str:
    return encode_message({"id": req_id, "result": result})


def error_line(req_id: Any, code: str, message: str) -> str:
    return encode_message({"id": req_id, "error": {"code": code, "message": message}})


def event_line(subscription_id: Any, kind: str, data: dict[str, Any]) -> str:
    """A server-initiated watch event, correlated to its ``watch_subscribe`` id.

    Events carry no top-level ``result``/``error``: they stream on the socket
    tied to a prior ``watch_subscribe`` request, keyed by ``subscription`` so one
    connection can multiplex the reply and its later event feed.
    """
    return encode_message({"event": kind, "subscription": subscription_id, "data": data})


def parse_message(line: str | bytes) -> dict[str, Any]:
    """Decode a line into a dict, raising ServeProtocolError on bad input."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ServeProtocolError(f"line is not valid UTF-8: {exc}") from exc
    stripped = line.strip()
    if not stripped:
        raise ServeProtocolError("empty line")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ServeProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ServeProtocolError(f"message must be an object, got {type(obj).__name__}")
    return obj


def parse_request(obj: dict[str, Any]) -> Request:
    if "method" not in obj:
        raise ServeProtocolError("request is missing 'method'")
    method = obj["method"]
    params = obj.get("params") or {}
    if not isinstance(params, dict):
        raise ServeProtocolError("'params' must be an object")
    return Request(id=obj.get("id"), method=method, params=params)


def parse_response(obj: dict[str, Any]) -> Response:
    result = obj.get("result")
    error = obj.get("error")
    if error is not None and not (isinstance(error, dict) and "code" in error):
        raise ServeProtocolError("error response must carry a 'code'")
    return Response(id=obj.get("id"), result=result, error=error)
