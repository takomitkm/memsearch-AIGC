# memsearch — ZCode Plugin

Automatic persistent memory for [ZCode](https://github.com/zcode-ai/zcode). Every conversation turn is summarized and indexed — your next session picks up where you left off.

## How it works

| When | What |
|------|------|
| Session starts | Indexes the project memory and writes the session heading |
| Each prompt | The user's prompt is cached so the Stop hook can attribute the turn |
| Turn ends (Stop) | The rollout's assistant text plus the cached prompt are summarized and saved to a daily `.md` file |

ZCode **redacts the request messages** from its rollout files, so the user's
question is captured by the UserPromptSubmit hook (the only place it exists)
rather than parsed from the transcript — the opposite trade-off from the
Codex plugin, which parses everything from its rollout.

## Prerequisites

- ZCode (with hook support: the seven hook events incl. `Stop` and `UserPromptSubmit`)
- Python 3.10+ (hooks call `python3`; on Windows Git Bash provides the shell)
- Git Bash / bash for the hook scripts (ZCode runs `command` hooks through a shell)

## Install

```bash
# 1. Install memsearch
uv tool install "memsearch[onnx]"

# 2. Clone the repo and run the installer
git clone -b Individual-mcp https://github.com/takomitkm/memsearch-AIGC.git
cd memsearch-AIGC
bash plugins/zcode/scripts/install.sh
```

The installer sets up everything automatically:
- Copies the **memory-recall / memory-config / memory-to-skill** skills to `~/.zcode/skills/`
- Registers **SessionStart / UserPromptSubmit / Stop** hooks in `~/.zcode/cli/config.json`
  (with `hooks.enabled: true` and a `config.json.bak` backup)

Then restart ZCode or start a new session so the hook runner picks up the config.

## Output contract

The hooks are **silent by design**: ZCode validates hook stdout against a
strict schema, and this plugin needs no output — capture happens in a
detached worker, so turns never wait on the summarizer.

## Search past memories

Use the `memory-recall` skill:

```
memory-recall what did we discuss about batch size limits?
```

ZCode will search your memory, expand relevant results, and return a curated
summary. The skill uses three-layer progressive disclosure:

1. **Search** — `memsearch search` finds relevant chunks by semantic + keyword hybrid search
2. **Expand** — `memsearch expand` retrieves the full markdown section around a match
3. **Deep drill** — rollout anchors reference the original ZCode rollout transcript

## Configuration

### Embedding provider

Default is `onnx` (bge-m3) — runs locally, no API key needed.

```bash
# Switch to OpenAI embeddings (requires OPENAI_API_KEY)
memsearch config set embedding.provider openai

# Switch back to local ONNX
memsearch config set embedding.provider onnx
```

### Milvus backend

Default is Milvus Lite (local `.db` file). For larger memory stores or team sharing:

```bash
# Use a remote Milvus server
memsearch config set milvus.uri http://localhost:19530
```

### Summarization routing

The Stop hook uses its built-in ZCode summarize model by default. To override only this plugin's native capture model:

```bash
memsearch config set plugins.zcode.summarize.model gpt-5.1-zcode-mini
```

To use a memsearch-managed API provider instead:

```bash
memsearch config set llm.providers.openai.type openai
memsearch config set llm.providers.openai.model gpt-5-mini
memsearch config set llm.providers.openai.api_key env:OPENAI_API_KEY
memsearch config set plugins.zcode.summarize.provider openai
```

Leave `plugins.zcode.summarize.provider` empty or set it to `native` to keep the current default. This setting does not fall back to `llm.model`.

## Memory files

Each project stores memory under `<project>/.memsearch/memory/`:

```
.memsearch/memory/
├── 2026-03-20.md
├── 2026-03-21.md
└── 2026-03-24.md
```

These are plain markdown files — human-readable, editable, and version-controllable. Milvus is a derived index that can be rebuilt anytime from these files.

## Uninstall

Remove only memsearch's hook entries from `~/.zcode/hooks.json` so unrelated ZCode hooks stay intact. The safe uninstall steps are documented in [docs/platforms/zcode/installation.md](../../docs/platforms/zcode/installation.md).

## Updating

```bash
cd memsearch
git pull
bash plugins/zcode/scripts/install.sh
```
