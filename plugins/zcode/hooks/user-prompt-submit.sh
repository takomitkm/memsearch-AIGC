#!/usr/bin/env bash
# UserPromptSubmit hook (ZCode): cache the user's prompt for the Stop hook.
#
# ZCode's rollout files redact the request messages, so this event is the only
# reliable place the user's question exists. The prompt is cached under
# ~/.memsearch/.zcode/ keyed by session id; hooks/stop.sh picks it up when the
# turn completes and prunes old state files.
#
# Silent by design: empty output is valid for a ZCode hook, and the plugin's
# recall surface is the memory-recall skill, not per-prompt injection.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# The prompt text arrives on stdin raw; tolerate a {"prompt": ...} JSON body too.
PROMPT="$INPUT"
if [ -z "$PROMPT" ]; then
  exit 0
fi

if printf '%s' "$PROMPT" | python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
except Exception:
    raise SystemExit(1)
if isinstance(obj, dict) and isinstance(obj.get("prompt"), str):
    sys.stdout.write(obj["prompt"])
    raise SystemExit(0)
raise SystemExit(1)
' > /tmp/memsearch-zcode-prompt.$$ 2>/dev/null; then
  PROMPT="$(cat /tmp/memsearch-zcode-prompt.$$)"
fi
rm -f /tmp/memsearch-zcode-prompt.$$ 2>/dev/null

PROMPT=$(printf '%s' "$PROMPT" | tr -d '\000')
if [ -z "$PROMPT" ]; then
  exit 0
fi

SESSION_ID="${ZCODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
if [ -z "$SESSION_ID" ]; then
  exit 0
fi

STATE_DIR="$HOME/.memsearch/.zcode"
mkdir -p "$STATE_DIR" 2>/dev/null
printf '%s' "$PROMPT" > "$STATE_DIR/last-prompt.${SESSION_ID}" 2>/dev/null

# Keep the state directory bounded: only the 20 most recent prompts survive.
ls -t "$STATE_DIR"/last-prompt.* 2>/dev/null | tail -n +21 | while read -r old; do
  rm -f "$old" 2>/dev/null
done

exit 0
