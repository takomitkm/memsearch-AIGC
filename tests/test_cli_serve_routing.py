"""CLI routing tests: daemon auto-routing rules and exit codes.

The commands themselves are exercised with a fake thin client, so nothing
touches Milvus, the embedding provider or a real daemon.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import memsearch.serve.client as client_mod
from memsearch.cli import _daemon_client_or_none, cli
from memsearch.serve.client import DaemonDownError


class FakeClient:
    def __init__(self, result=None, error=None) -> None:
        self.result = result or {}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.result


def test_routing_refused_with_store_overrides(monkeypatch) -> None:
    monkeypatch.setattr(client_mod, "daemon_client_if_alive", lambda collection: "client")
    assert _daemon_client_or_none("c", False, None, "override") is None
    assert _daemon_client_or_none("c", True) is None
    assert _daemon_client_or_none("c", False, None, None) == "client"


def test_search_routes_through_a_live_daemon(monkeypatch) -> None:
    fake = FakeClient({"results": [{"score": 0.5, "source": "x.md", "content": "hi"}]})
    monkeypatch.setattr(client_mod, "daemon_client_if_alive", lambda collection: fake)
    result = CliRunner().invoke(cli, ["search", "hello"])
    assert result.exit_code == 0, result.output
    assert "x.md" in result.output
    assert fake.calls and fake.calls[0][0] == "search"
    assert fake.calls[0][1]["query"] == "hello"


def test_stats_routes_through_a_live_daemon(monkeypatch) -> None:
    fake = FakeClient({"total_chunks": 42, "sources": 1, "source_list": ["a.md"]})
    monkeypatch.setattr(client_mod, "daemon_client_if_alive", lambda collection: fake)
    result = CliRunner().invoke(cli, ["stats"])
    assert result.exit_code == 0, result.output
    assert "Total indexed chunks: 42" in result.output
    assert "a.md" in result.output


def test_no_daemon_flag_skips_routing(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(client_mod, "daemon_client_if_alive", lambda collection: seen.append(collection) or FakeClient())
    # The command proceeds down the local path, which cannot reach this store —
    # the point is only that routing was skipped.
    result = CliRunner().invoke(cli, ["stats", "--no-daemon", "--milvus-uri", "S:\\definitely\\not\\here\\x.db"])
    assert seen == []
    assert result.exit_code != 0


def test_dead_daemon_exits_with_code_two(monkeypatch) -> None:
    fake = FakeClient(error=DaemonDownError("127.0.0.1:1", None))
    monkeypatch.setattr(client_mod, "daemon_client_if_alive", lambda collection: fake)
    result = CliRunner().invoke(cli, ["search", "hello"])
    assert result.exit_code == 2
    assert "daemon" in result.output.lower()
