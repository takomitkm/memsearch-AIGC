"""Tests for the ZCode rollout parser and the plugin's hooks manifest."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("plugins/zcode/scripts/parse-zcode-rollout.sh")
MANIFEST = Path("plugins/zcode/hooks/hooks.json")


def _model_io(turn: str, text: str = "", tool_call: bool = False) -> dict:
    response: dict = {"finishReason": "stop"}
    if text:
        response["text"] = text
    if tool_call:
        response["toolCalls"] = [{"name": "Bash"}]
    return {
        "type": "model_io",
        "turnId": turn,
        "sessionId": "sess_test",
        "request": {"body": {"model": "m", "system": [], "tools": []}},
        "response": response,
    }


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _run_parse(path: Path) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT), str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def test_parse_returns_only_the_last_turn(tmp_path: Path) -> None:
    rollout = tmp_path / "model-io-sess_test.jsonl"
    _write_jsonl(
        rollout,
        [
            _model_io("turn_old", "stale answer one"),
            _model_io("turn_old", "stale answer two"),
            _model_io("turn_new", "final answer"),
        ],
    )
    output = _run_parse(rollout)

    assert output.startswith("=== ZCode rollout turn ===")
    assert "[ZCode]: final answer" in output
    assert "stale" not in output


def test_parse_joins_multi_step_assistant_text(tmp_path: Path) -> None:
    rollout = tmp_path / "model-io-sess_test.jsonl"
    _write_jsonl(
        rollout,
        [
            _model_io("turn_1", "step one text"),
            _model_io("turn_1", "", tool_call=True),  # tool-call step: no text
            _model_io("turn_1", "step two text"),
        ],
    )
    output = _run_parse(rollout)

    assert output.count("[ZCode]:") == 2
    assert "[ZCode]: step one text" in output
    assert "[ZCode]: step two text" in output


def test_parse_empty_and_edge_cases(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert _run_parse(empty).strip() == "(empty rollout)"

    no_records = tmp_path / "other.jsonl"
    _write_jsonl(no_records, [{"type": "unrelated", "note": "x"}])
    assert _run_parse(no_records).strip() == "(no model_io records)"

    silent = tmp_path / "silent.jsonl"
    _write_jsonl(silent, [_model_io("turn_1", "")])
    assert _run_parse(silent).strip() == "(no assistant text in last turn)"


def test_missing_rollout_path_errors() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            ["bash", str(SCRIPT), str(Path("S:/definitely/not/here.jsonl"))],
            check=True,
            capture_output=True,
            text=True,
        )


def test_hooks_manifest_declares_the_three_events() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    events = data["hooks"]
    assert set(events) == {"SessionStart", "UserPromptSubmit", "Stop"}
    for event, groups in events.items():
        assert groups, f"{event} has no hook groups"
        for group in groups:
            for hook in group["hooks"]:
                assert hook["type"] == "process"
                assert hook["command"] == "bash"
                assert hook["args"][0].startswith("${ZCODE_PLUGIN_ROOT}/hooks/")
