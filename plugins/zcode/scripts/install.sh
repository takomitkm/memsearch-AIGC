#!/usr/bin/env bash
# One-click installer for the memsearch ZCode plugin.
# Copies the skills into ZCode's skill directory and registers the Stop /
# UserPromptSubmit / SessionStart hooks in ~/.zcode/cli/config.json.
#
# Usage: bash plugins/zcode/scripts/install.sh

set -euo pipefail

# Determine install directory (parent of scripts/)
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ZCODE_DIR="${ZCODE_CLI_HOME:-$HOME/.zcode/cli}"
CONFIG_FILE="$ZCODE_DIR/config.json"
SKILLS_DIR="$HOME/.zcode/skills"

replace_text_in_file() {
  local target_file="$1"
  local old_text="$2"
  local new_text="$3"

  python3 - "$target_file" "$old_text" "$new_text" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
old = sys.argv[2]
new = sys.argv[3]

text = path.read_text()
path.write_text(text.replace(old, new))
PY
}

install_or_update_hooks() {
  local config_file="$1"
  local install_dir="$2"

  python3 - "$config_file" "$install_dir" <<'PY'
from pathlib import Path
import json
import os
import sys

config_file = Path(sys.argv[1])
install_dir = sys.argv[2]

spec = {
    "SessionStart": {"script": "session-start.sh", "timeout": 60},
    "UserPromptSubmit": {"script": "user-prompt-submit.sh", "timeout": 15},
    "Stop": {"script": "stop.sh", "timeout": 15},
}

MARKER = "plugins/zcode/hooks/"


def strip_old_memsearch(events):
    """Remove previously installed memsearch-zcode hook entries."""
    cleaned = {}
    for event, groups in (events or {}).items():
        kept = []
        for group in groups or []:
            hooks = []
            for hook in (group or {}).get("hooks", []):
                command = " ".join([hook.get("command", "")] + [str(a) for a in hook.get("args", [])]) if isinstance(hook, dict) else ""
                if MARKER in command:
                    continue
                hooks.append(hook)
            if hooks:
                copied = dict(group)
                copied["hooks"] = hooks
                kept.append(copied)
        if kept:
            cleaned[event] = kept
    return cleaned


data = {}
if config_file.exists():
    try:
        data = json.loads(config_file.read_text())
    except Exception:
        backup = config_file.with_suffix(".json.bak-unparseable")
        backup.write_text(config_file.read_text())
        print(f"  ⚠ Existing config.json was unparseable — backed up to {backup.name}")
        data = {}
if not isinstance(data, dict):
    data = {}

hooks = data.setdefault("hooks", {})
hooks["enabled"] = True
events = hooks.setdefault("events", {})
for event, groups in list(events.items()):
    events[event] = strip_old_memsearch([groups] if isinstance(groups, dict) else groups)

for event, details in spec.items():
    entry = {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": f'bash "{install_dir}/hooks/{details["script"]}"',
                "timeout": details["timeout"],
            }
        ],
    }
    existing = events.get(event, [])
    existing.append(entry)
    events[event] = existing

# Write via sibling temp + os.replace so an interrupted write never leaves
# config.json truncated.
tmp = config_file.with_name(config_file.name + ".tmp")
tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
os.replace(tmp, config_file)
PY
}

echo "=== memsearch ZCode Plugin Installer ==="
echo "Install directory: $INSTALL_DIR"
echo ""

# --- 1. Check memsearch availability ---
echo "[1/5] Checking memsearch..."
if command -v memsearch &>/dev/null; then
  MS_VERSION=$(memsearch --version 2>/dev/null || echo "unknown")
  echo "  ✓ memsearch found: $(command -v memsearch) ($MS_VERSION)"
elif command -v uvx &>/dev/null; then
  echo "  ✓ uvx found — will use: uvx --from memsearch[onnx] memsearch"
  echo "  Warming up cache (first run may take ~30s)..."
  uvx --from 'memsearch[onnx]' memsearch --version 2>/dev/null || true
else
  echo "  ✗ memsearch not found. Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  echo "  Warming up uvx cache (first run may take ~30s)..."
  uvx --from 'memsearch[onnx]' memsearch --version 2>/dev/null || true
fi

# --- 2. Install skills ---
echo "[2/5] Installing memsearch skills..."
mkdir -p "$SKILLS_DIR"

for skill_name in memory-recall memory-config memory-to-skill; do
  SKILL_SRC="$INSTALL_DIR/skills/$skill_name"
  SKILL_DST="$SKILLS_DIR/$skill_name"
  if [ -d "$SKILL_DST" ] || [ -L "$SKILL_DST" ]; then
    echo "  ⚠ Existing $skill_name skill found — replacing"
    rm -rf "$SKILL_DST"
  fi

  # Copy (not symlink) so we can substitute __INSTALL_DIR__ placeholder
  cp -r "$SKILL_SRC" "$SKILL_DST"
  echo "  ✓ Copied $skill_name skill to $SKILL_DST"
done

# --- 3. Replace __INSTALL_DIR__ placeholder in SKILL.md ---
echo "[3/5] Configuring skill paths..."
for skill_name in memory-recall memory-config memory-to-skill; do
  SKILL_DST="$SKILLS_DIR/$skill_name"
  if [ -f "$SKILL_DST/SKILL.md" ]; then
    replace_text_in_file "$SKILL_DST/SKILL.md" "__INSTALL_DIR__" "$INSTALL_DIR"
    echo "  ✓ Updated $skill_name SKILL.md with install path: $INSTALL_DIR"
  fi
done

# --- 4. Register hooks in ZCode config ---
echo "[4/5] Registering hooks..."
if [ ! -f "$CONFIG_FILE" ]; then
  mkdir -p "$ZCODE_DIR"
  echo '{}' > "$CONFIG_FILE"
else
  cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
  echo "  ⚠ Existing config.json found — backed up to config.json.bak"
fi
install_or_update_hooks "$CONFIG_FILE" "$INSTALL_DIR"
echo "  ✓ Installed memsearch-zcode hooks in $CONFIG_FILE"

# --- 5. Make scripts executable ---
echo "[5/5] Setting permissions..."
chmod +x "$INSTALL_DIR/hooks/"*.sh 2>/dev/null
chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null
echo "  ✓ All scripts marked executable"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "The memsearch plugin is now configured for ZCode. Restart ZCode (or start"
echo "a new session) so the hook runner picks up the config change."
echo ""
echo "What happens automatically:"
echo "  • SessionStart: indexes project memory, writes the session heading"
echo "  • Stop: summarizes each turn and saves it to memory"
echo "  • UserPromptSubmit: caches the user's prompt for the Stop summary"
echo "  • memory-recall skill: search past memories when relevant"
echo "  • memory-config skill: diagnose and configure memsearch"
echo "  • memory-to-skill skill: turn recurring workflows into reusable skills"
echo ""
echo "Memory files:   <project>/.memsearch/memory/*.md"
echo "Hooks config:   $CONFIG_FILE (hooks.enabled = true)"
echo "Skill location: $SKILLS_DIR/{memory-recall,memory-config,memory-to-skill}"
echo ""
echo "To verify: start a new ZCode session and look for a new '## Session'"
echo "heading in <project>/.memsearch/memory/."
