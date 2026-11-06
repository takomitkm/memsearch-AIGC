"""Tests for the framework-backed MCP backends (``fastmcp`` and the official SDK).

The suite drives each server over real stdio with hand-written JSON-RPC, so it
works for any framework implementation and needs no framework-specific client.
A backend whose optional dependency is missing is skipped, not failed — the
stdlib server remains the default and is covered by the existing MCP tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE_TOOLS = {
    "memory_search",
    "memory_index",
    "memory_stats",
    "memory_expand",
    "embed",
    "rerank",
}


def _framework_available(backend: str) -> bool:
    module = "fastmcp" if backend == "fastmcp" else "mcp.server.mcpserver"
    probe = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def _server_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _rpc(backend: str, requests: list[dict]) -> list[dict]:
    """Run ``memsearch mcp --backend <backend>`` and exchange JSON-RPC lines."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "from memsearch.cli import cli; cli()", "mcp", "--backend", backend],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_server_env(),
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    replies: list[dict] = []
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                break
            replies.append(json.loads(line))
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
    return replies


def _initialize() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "backends-test", "version": "0"},
        },
    }


def _list_tools() -> dict:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.mark.parametrize("backend", ["fastmcp", "official"])
def test_backend_lists_the_core_tools(backend: str) -> None:
    if not _framework_available(backend):
        pytest.skip(f"{backend} is not installed")
    replies = _rpc(backend, [_initialize(), _list_tools()])
    assert len(replies) == 2, replies
    assert replies[0]["result"]["serverInfo"]["name"] == "memsearch"
    tools = replies[1]["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names >= CORE_TOOLS, names


@pytest.mark.parametrize("backend", ["fastmcp", "official"])
def test_backend_reports_tool_annotations(backend: str) -> None:
    if not _framework_available(backend):
        pytest.skip(f"{backend} is not installed")
    replies = _rpc(backend, [_initialize(), _list_tools()])
    tools = {tool["name"]: tool for tool in replies[1]["result"]["tools"]}
    search = tools["memory_search"]
    assert search["annotations"]["readOnlyHint"] is True
    assert tools["memory_index"]["annotations"]["idempotentHint"] is True


@pytest.mark.parametrize("backend", ["fastmcp", "official"])
def test_backend_turns_bad_arguments_into_a_tool_error(backend: str) -> None:
    if not _framework_available(backend):
        pytest.skip(f"{backend} is not installed")
    replies = _rpc(
        backend,
        [
            _initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "memory_search", "arguments": {"query": ""}},
            },
        ],
    )
    result = replies[1]["result"]
    assert result["isError"] is True
    assert "query" in result["content"][0]["text"]


def test_both_backends_expose_the_same_tool_surface() -> None:
    """The two frameworks must not drift: same names, same required arguments."""
    surfaces = {}
    for backend in ("fastmcp", "official"):
        if not _framework_available(backend):
            pytest.skip(f"{backend} is not installed")
        replies = _rpc(backend, [_initialize(), _list_tools()])
        surfaces[backend] = {
            tool["name"]: sorted((tool.get("inputSchema") or {}).get("required") or [])
            for tool in replies[1]["result"]["tools"]
        }
    assert surfaces["fastmcp"] == surfaces["official"]
    assert set(surfaces["fastmcp"]) >= CORE_TOOLS


def test_help_lists_the_three_backends() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from memsearch.cli import cli; cli()", "mcp", "--help"],
        capture_output=True,
        text=True,
        env=_server_env(),
    )
    assert result.returncode == 0, result.stderr
    for backend in ("stdlib", "fastmcp", "official"):
        assert backend in result.stdout
