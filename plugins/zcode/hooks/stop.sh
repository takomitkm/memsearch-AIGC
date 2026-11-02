#!/usr/bin/env bash
# Stop hook (ZCode): capture the finished turn into memsearch memory.
#
# ZCode hands the hook the assistant's response preview on stdin (raw text) and
# exposes the session id as ZCODE_SESSION_ID / CLAUDE_SESSION_ID. The rollout
# file at ~/.zcode/cli/rollout/model-io-<session>.jsonl carries the full
# assistant text for the turn — ZCode redacts the request messages, so the
# user's question is cached by the UserPromptSubmit hook into a state file
# instead.
#
# Async: hooks run inline in ZCode (the `async` field has no effect), so this
# prints nothing and detaches a worker to keep the session snappy.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_required_env_var() {
  case "$1" in
    openai) echo "OPENAI_API_KEY" ;;
    google) echo "GOOGLE_API_KEY" ;;
    voyage) echo "VOYAGE_API_KEY" ;;
    jina) echo "JINA_API_KEY" ;;
    mistral) echo "MISTRAL_API_KEY" ;;
    *) echo "" ;;  # onnx, ollama, local — no API key needed
  esac
}

_truncate_chars() {
  local limit="$1"
  python3 -c "
import sys
limit = int(sys.argv[1])
text = sys.stdin.read()
sys.stdout.write(text[:limit])
" "$limit" 2>/dev/null || true
}

_valid_utf8() {
  iconv -f UTF-8 -t UTF-8 -c 2>/dev/null || cat
}

run_worker() {
  local work_file="${1:-}"
  if [ -z "$work_file" ] || [ ! -f "$work_file" ]; then
    exit 0
  fi

  export MEMSEARCH_SKIP_HOOK_STDIN=1
  source "$SCRIPT_DIR/common.sh"

  if memsearch_available && ! require_default_collection_support; then
    exit 0
  fi

  local work_input
  work_input=$(cat "$work_file" 2>/dev/null || echo "")
  rm -f "$work_file"
  if [ -z "$work_input" ]; then
    exit 0
  fi

  local NOW MEMORY_FILE SESSION_ID ROLLOUT_PATH CONTENT USER_QUESTION RESPONSE_PREVIEW
  NOW=$(_load_worker_field "$work_input" "now")
  MEMORY_FILE=$(_load_worker_field "$work_input" "memory_file")
  SESSION_ID=$(_load_worker_field "$work_input" "session_id")
  ROLLOUT_PATH=$(_load_worker_field "$work_input" "rollout_path")
  CONTENT=$(_load_worker_field "$work_input" "content")
  USER_QUESTION=$(_load_worker_field "$work_input" "user_question")
  RESPONSE_PREVIEW=$(_load_worker_field "$work_input" "response_preview")

  if [ -z "$MEMORY_FILE" ] || [ -z "$CONTENT" ]; then
    exit 0
  fi

  ensure_memory_dir

  local SUMMARIZE_ENABLED="true"
  if memsearch_available; then
    SUMMARIZE_ENABLED=$(_memsearch config get plugins.zcode.summarize.enabled 2>/dev/null || echo "true")
  fi
  if [ "$SUMMARIZE_ENABLED" = "false" ]; then
    return 0
  fi

  # Load summarization prompt: user custom (via config) > plugin built-in template
  local AGENT_NAME="ZCode"
  local PROMPT_FILE=""
  if memsearch_available; then
    PROMPT_FILE=$(_memsearch config get prompts.summarize 2>/dev/null || true)
    [ -n "$PROMPT_FILE" ] && PROMPT_FILE=$(project_path "$PROMPT_FILE")
  fi
  local SYSTEM_PROMPT=""
  if [ -n "$PROMPT_FILE" ] && [ -f "$PROMPT_FILE" ]; then
    SYSTEM_PROMPT=$(sed "s/{{AGENT_NAME}}/$AGENT_NAME/g" "$PROMPT_FILE")
  elif [ -f "$SCRIPT_DIR/../prompts/summarize.txt" ]; then
    SYSTEM_PROMPT=$(sed "s/{{AGENT_NAME}}/$AGENT_NAME/g" "$SCRIPT_DIR/../prompts/summarize.txt")
  else
    SYSTEM_PROMPT="You are a third-person note-taker. Summarize the transcript as 2-10 bullet points. Write in third person. Mandatory language rule: write every bullet in the same primary language as the [User] text. If User mixes languages, use the dominant user-facing language. Do NOT answer User's question. Output ONLY bullet points."
  fi

  local SUMMARY=""
  local SUMMARIZE_PROVIDER=""
  if memsearch_available; then
    SUMMARIZE_PROVIDER=$(_memsearch config get plugins.zcode.summarize.provider 2>/dev/null || true)
  fi

  # Only the memsearch-managed provider is available here: unlike Codex there
  # is no headless ZCode CLI to fall back on, so without a configured provider
  # the capture falls back to the raw exchange below.
  if [ -n "$SUMMARIZE_PROVIDER" ] && [ "$SUMMARIZE_PROVIDER" != "native" ] && memsearch_available; then
    SUMMARY=$(printf '%s' "$CONTENT" | MEMSEARCH_NO_WATCH=1 MEMSEARCH_IN_STOP_WORKER=1 _memsearch summarize \
      --plugin zcode \
      --agent-name "$AGENT_NAME" \
      2>/dev/null || true)
  fi

  if [ -z "$SUMMARY" ]; then
    if [ -n "$USER_QUESTION" ] && [ -n "$RESPONSE_PREVIEW" ]; then
      local TRUNCATED_MSG
      TRUNCATED_MSG=$(printf '%s' "$RESPONSE_PREVIEW" | _truncate_chars 800)
      if [ ${#RESPONSE_PREVIEW} -gt 800 ]; then
        TRUNCATED_MSG="${TRUNCATED_MSG}..."
      fi
      SUMMARY="- User asked: ${USER_QUESTION}
- ZCode: ${TRUNCATED_MSG}"
    elif [ -n "$RESPONSE_PREVIEW" ]; then
      local TRUNCATED_MSG
      TRUNCATED_MSG=$(printf '%s' "$RESPONSE_PREVIEW" | _truncate_chars 800)
      if [ ${#RESPONSE_PREVIEW} -gt 800 ]; then
        TRUNCATED_MSG="${TRUNCATED_MSG}..."
      fi
      SUMMARY="- ZCode: ${TRUNCATED_MSG}"
    else
      SUMMARY="$CONTENT"
    fi
  fi

  {
    echo "### $NOW"
    if [ -n "$SESSION_ID" ]; then
      echo "<!-- session:${SESSION_ID} rollout:${ROLLOUT_PATH} -->"
    fi
    printf '%s\n' "$SUMMARY" | _valid_utf8
    echo ""
  } >> "$MEMORY_FILE"

  local _uri
  _uri="${MILVUS_URI:-$(_memsearch config get milvus.uri 2>/dev/null || echo "")}"
  if [[ "$_uri" == http* ]] || [[ "$_uri" == tcp* ]] || [[ "$_uri" == unix:* ]]; then
    kill_orphaned_index
    run_memsearch index "$MEMORY_DIR" >/dev/null
  fi

  run_maintenance
}

if [ "${1:-}" = "--worker" ]; then
  run_worker "${2:-}"
  exit 0
fi

source "$SCRIPT_DIR/common.sh"

# Recursion guard: the worker exports MEMSEARCH_IN_STOP_WORKER across its own
# subprocesses, so a nested Stop can never spawn another worker.
if [ -n "${MEMSEARCH_IN_STOP_WORKER:-}" ]; then
  exit 0
fi

if memsearch_available && ! require_default_collection_support; then
  exit 0
fi

# Skip summarization when the required API key is missing
_PROVIDER=$(_memsearch config get embedding.provider 2>/dev/null || echo "onnx")
_REQ_KEY=$(_required_env_var "$_PROVIDER")
if [ -n "$_REQ_KEY" ] && [ -z "${!_REQ_KEY:-}" ]; then
  _CONFIG_API_KEY=""
  if memsearch_available; then
    _CONFIG_API_KEY=$(_memsearch config get embedding.api_key 2>/dev/null || echo "")
  fi
  if [ -z "$_CONFIG_API_KEY" ]; then
    exit 0
  fi
fi

# Response preview arrives on stdin as raw text (ZCode Stop event).
RESPONSE_PREVIEW="$INPUT"

ensure_memory_dir

TODAY=$(date +%Y-%m-%d)
NOW=$(date +%H:%M)
MEMORY_FILE="$MEMORY_DIR/$TODAY.md"

# Session id: ZCode exposes it as an env var; fall back to the newest rollout.
SESSION_ID="${ZCODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
ROLLOUT_DIR="${ZCODE_CLI_HOME:-$HOME/.zcode/cli}/rollout"
if [ -z "$SESSION_ID" ]; then
  NEWEST=$(ls -t "$ROLLOUT_DIR"/model-io-*.jsonl 2>/dev/null | head -n 1)
  [ -n "$NEWEST" ] && SESSION_ID=$(basename "$NEWEST" .jsonl | sed 's/^model-io-//')
fi

USER_QUESTION=""
PROMPT_STATE="$HOME/.memsearch/.zcode/last-prompt.${SESSION_ID}"
if [ -n "$SESSION_ID" ] && [ -f "$PROMPT_STATE" ]; then
  USER_QUESTION=$(head -n 1 "$PROMPT_STATE" 2>/dev/null | cut -c1-200)
fi

ROLLOUT_PATH=""
PARSED=""
if [ -n "$SESSION_ID" ]; then
  ROLLOUT_PATH="${ROLLOUT_DIR}/model-io-${SESSION_ID}.jsonl"
  if [ ! -f "$ROLLOUT_PATH" ]; then
    ROLLOUT_PATH=$(ls -t "$ROLLOUT_DIR"/model-io-*"${SESSION_ID}"*.jsonl 2>/dev/null | head -n 1)
  fi
fi

if [ -n "$ROLLOUT_PATH" ] && [ -f "$ROLLOUT_PATH" ]; then
  PARSED=$("$SCRIPT_DIR/../scripts/parse-zcode-rollout.sh" "$ROLLOUT_PATH" 2>/dev/null || true)
fi

CONTENT=""
if [ -n "$USER_QUESTION" ] && [ -n "$RESPONSE_PREVIEW" ]; then
  CONTENT="=== Final exchange, authoritative for outcome ===
[User]: ${USER_QUESTION}
[ZCode final]: ${RESPONSE_PREVIEW}"
  if [ -n "$PARSED" ] && [ "$PARSED" != "(empty rollout)" ] && [ "$PARSED" != "(no model_io records)" ] && [ "$PARSED" != "(no assistant text in last turn)" ]; then
    CONTENT="${CONTENT}

=== Additional conversation context ===
${PARSED}"
  fi
elif [ -n "$PARSED" ] && [ "$PARSED" != "(empty rollout)" ] && [ "$PARSED" != "(no model_io records)" ] && [ "$PARSED" != "(no assistant text in last turn)" ]; then
  CONTENT="$PARSED"
elif [ -n "$USER_QUESTION" ]; then
  CONTENT="[User]: ${USER_QUESTION}"
else
  exit 0
fi

MAX_CONTENT_CHARS="${MEMSEARCH_SUMMARY_MAX_CHARS:-8000}"
if [ ${#CONTENT} -gt "$MAX_CONTENT_CHARS" ]; then
  CONTENT="$(printf '%s' "$CONTENT" | _truncate_chars "$MAX_CONTENT_CHARS")...(truncated)"
fi

WORK_FILE="$(mktemp "${TMPDIR:-/tmp}/memsearch-zcode-stop.XXXXXX")"
python3 - "$WORK_FILE" "$NOW" "$MEMORY_FILE" "$SESSION_ID" "$ROLLOUT_PATH" "$CONTENT" "$USER_QUESTION" "$RESPONSE_PREVIEW" <<'PY'
from pathlib import Path
import json
import sys

payload = {
    "now": sys.argv[2],
    "memory_file": sys.argv[3],
    "session_id": sys.argv[4],
    "rollout_path": sys.argv[5],
    "content": sys.argv[6],
    "user_question": sys.argv[7],
    "response_preview": sys.argv[8],
}
Path(sys.argv[1]).write_text(json.dumps(payload))
PY

# Silent: ZCode's hook output schema is strict and empty output is valid.

if command -v setsid &>/dev/null; then
  MEMSEARCH_PROJECT_DIR="$PROJECT_DIR" MEMSEARCH_SKIP_HOOK_STDIN=1 setsid bash "$0" --worker "$WORK_FILE" </dev/null &>/dev/null &
else
  MEMSEARCH_PROJECT_DIR="$PROJECT_DIR" MEMSEARCH_SKIP_HOOK_STDIN=1 nohup bash "$0" --worker "$WORK_FILE" </dev/null &>/dev/null &
fi
