#!/usr/bin/env bash
# Parse a ZCode CLI rollout JSONL — extract the LAST TURN's assistant text.
#
# A ZCode rollout (model-io-*.jsonl) records one `model_io` record per LLM API
# call, with `turnId` grouping records into turns:
#
#   { "type": "model_io", "turnId": "turn_<uuid>",
#     "request":  { "body": { "model", "system", "tools", ... } },   ← no messages
#     "response": { "text": "...", "finishReason", ... } }
#
# ZCode redacts the request messages (no user text in the file), so the parse
# yields the assistant side only: the non-empty `response.text` values of the
# last turn, in order. Tool-call steps have empty text and are skipped, so the
# summarizer works from the assistant's spoken output only.
#
# Usage: bash parse-zcode-rollout.sh <rollout_path>

set -euo pipefail

ROLLOUT_PATH="${1:-}"

if [ -z "$ROLLOUT_PATH" ] || [ ! -f "$ROLLOUT_PATH" ]; then
  echo "ERROR: rollout not found: $ROLLOUT_PATH" >&2
  exit 1
fi

LINE_COUNT=$(wc -l < "$ROLLOUT_PATH" 2>/dev/null || echo "0")
if [ "$LINE_COUNT" -eq 0 ]; then
  echo "(empty rollout)"
  exit 0
fi

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON_BIN" ] && command -v py >/dev/null 2>&1; then
  PYTHON_BIN="py -3"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: no python found on PATH (tried python3, python, py)" >&2
  exit 1
fi
# shellcheck disable=SC2086  # "py -3" intentionally word-splits
$PYTHON_BIN - "$ROLLOUT_PATH" <<'PY'
import json
import sys

path = sys.argv[1]
records = []
with open(path, encoding="utf-8", errors="replace") as f:
    for line in f:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("type") == "model_io":
            records.append(obj)

if not records:
    print("(no model_io records)")
    raise SystemExit(0)

last_turn = records[-1].get("turnId")
texts = []
for obj in records:
    if obj.get("turnId") != last_turn:
        continue
    text = ((obj.get("response") or {}).get("text") or "").strip()
    if text:
        texts.append(text)

if not texts:
    print("(no assistant text in last turn)")
    raise SystemExit(0)

output = ["=== ZCode rollout turn ==="]
for text in texts:
    output.append("[ZCode]: " + text)
print("\n\n".join(output))
PY
