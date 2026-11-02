"""Contract tests for the serve wire format — no daemon, no sockets."""

import json

import pytest

from memsearch.serve import protocol as proto


def test_encode_escaped_never_contains_newline():
    # Framing safety: a serialized message must never embed a raw newline, so
    # the line split on the socket is unambiguous even for CJK/emoji payloads.
    line = proto.response_line(1, {"content": "中文 \U0001f600\ntrailing"})
    assert "\n" not in line
    assert json.loads(line)["result"]["content"] == "中文 \U0001f600\ntrailing"


def test_request_response_round_trip():
    line = proto.request_line(7, "search", {"query": "why", "top_k": 3})
    obj = proto.parse_message(line)
    req = proto.parse_request(obj)
    assert req.id == 7 and req.method == "search" and req.params["top_k"] == 3


def test_error_response_round_trip():
    line = proto.error_line(2, proto.NOT_FOUND, "no such chunk")
    resp = proto.parse_response(proto.parse_message(line))
    assert resp.ok is False
    assert resp.error == {"code": "NOT_FOUND", "message": "no such chunk"}


def test_watch_subscribe_is_a_known_method():
    # METHODS is the shared vocabulary the client validates against.
    assert "watch_subscribe" in proto.METHODS
    line = proto.request_line(9, "watch_subscribe")
    assert proto.parse_message(line)["params"] == {}


def test_unknown_request_method_rejected():
    with pytest.raises(proto.ServeProtocolError, match="unknown method"):
        proto.request_line(1, "drop_everything")


def test_parse_rejects_non_object_and_empty():
    with pytest.raises(proto.ServeProtocolError, match="empty line"):
        proto.parse_message("   ")
    with pytest.raises(proto.ServeProtocolError, match="must be an object"):
        proto.parse_message("[1, 2, 3]")


def test_parse_accepts_utf8_bytes():
    raw = proto.response_line(3, "ok").encode("utf-8")
    assert proto.parse_message(raw)["result"] == "ok"


def test_daemon_down_is_never_on_the_wire():
    # DAEMON_DOWN is a client-local signal (no daemon answered); it is not sent
    # by the server, so the "service down vs empty result" distinction is local.
    assert proto.DAEMON_DOWN not in (proto.INVALID_PARAMS, proto.NOT_FOUND, proto.INTERNAL)
    line = proto.error_line(1, proto.DAEMON_DOWN, "unreachable")
    assert json.loads(line)["error"]["code"] == "DAEMON_DOWN"
