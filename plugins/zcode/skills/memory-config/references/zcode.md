# ZCode platform reference

Environment markers, config paths, and platform commands for running the
memsearch plugin inside [ZCode](https://github.com/zcode-ai/zcode).

## Environment markers

- `ZCODE_SESSION_ID` / `CLAUDE_SESSION_ID` — set inside hook processes; the
  session id matches the rollout file
  `~/.zcode/cli/rollout/model-io-<session>.jsonl`.
- `ZCODE_PLUGIN_ROOT` / `CLAUDE_PLUGIN_ROOT` — set only inside **plugin**
  hooks; points at the installed plugin directory.
- Config file: `~/.zcode/cli/config.json` (workspace: `<repo>/.zcode/config.json`).
- Rollout transcripts: `~/.zcode/cli/rollout/model-io-sess_<uuid>.jsonl`.

## Hooks

Configured in `config.json` under `hooks.events.<Event>`; **configuration-file
hooks require `hooks.enabled: true`** (the installer sets it). Event names,
exactly seven: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `Stop`.

Payloads: `UserPromptSubmit` → the prompt text on stdin; `Stop` → the response
preview on stdin (raw text). Hook stdout is parsed as JSON with a **strict
schema** — any unrecognized key fails validation — so this plugin's hooks stay
silent and rely on exit codes. Empty output is valid.

`command` hooks take `timeout` in **seconds** (`process` hooks take
`timeoutMs`); the default kill timeout is 60 s and the output cap is 32 KiB.
Cross-platform: prefer `process` hooks (no shell) or invoke scripts through an
explicit interpreter (`bash "..."`), which makes the executable bit irrelevant.

## Version-check command

```bash
memsearch --version
```

## Config keys

`plugins.zcode.summarize.enabled` (default `true`) and
`plugins.zcode.summarize.provider` in `~/.memsearch/config.toml` control the
Stop-hook summarizer, mirroring the other platforms. `native` is **not**
supported on ZCode (there is no headless ZCode CLI the plugin can call);
without a configured provider the capture falls back to the raw exchange.

## Restart guidance

Hooks are read from `config.json` when a session starts. After changing the
config or the plugin scripts, restart ZCode or start a new session.

## Install / uninstall

```bash
bash plugins/zcode/scripts/install.sh     # registers hooks + copies skills
```

Uninstall: remove the `plugins/zcode/hooks/` entries from
`~/.zcode/cli/config.json` (`hooks.events.*`), delete
`~/.zcode/skills/{memory-recall,memory-config,memory-to-skill}`, and set
`hooks.enabled: false` if no other hooks remain.
