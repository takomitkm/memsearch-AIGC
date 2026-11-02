# Serve daemon (`memsearch serve`)

A resident daemon that owns one collection's store and embedding model, so
repeated queries stay warm. It answers the request in upstream issue
[#780](https://github.com/zilliztech/memsearch/issues/780):

* **One process owns the store.** Milvus Lite takes an exclusive file lock, so
  `memsearch watch` and a separate `memsearch search` cannot share a
  collection. With a daemon, every client goes through the one owner.
* **No model reload per call.** A cold CLI search pays ~2.2 s to load the
  embedding model; a warm daemon answers in ~50–100 ms.
* **A dead service is not an empty result.** Clients exit with code **2**
  (`EXIT_DAEMON_DOWN`) instead of returning "no matches".

## Quick start

```bash
memsearch serve start ~/notes        # foreground; run under tmux/screen to detach
memsearch serve status               # is it up? which endpoint, uptime, watcher state
memsearch search "cache policy"      # automatically routed through the daemon
memsearch serve stop                 # graceful shutdown
```

## Commands

| Command | What it does |
|---|---|
| `memsearch serve start [PATHS...]` | Index and watch `PATHS` (default: the current directory). Foreground only — there is no `--daemonize`; use your terminal multiplexer or an OS service manager. |
| `memsearch serve status` | Prints endpoint, collection, uptime, watcher/subscriber counts. Exits 2 when no daemon is running. |
| `memsearch serve stop` | Sends `shutdown`; the daemon stops the watcher, closes the store and removes its pidfile. Exits 2 when no daemon is running. |

Options on `start`: `--socket PATH` (Unix), `--tcp-port PORT`, `--http-port PORT`,
`--pidfile PATH`, plus the usual config overrides (`-c`, `--milvus-uri`, …).

## Endpoints

| Transport | Default | Notes |
|---|---|---|
| Unix socket | `~/.memsearch/serve-<collection>.sock` | POSIX default. Keyed by collection, so two collections get two independent daemons. |
| TCP | `127.0.0.1:19834` | Windows default (no Unix sockets there), or explicit `--tcp-port`. Loopback only. |
| HTTP | off; `--http-port PORT` | `POST /rpc` with the same `{id, method, params}` envelope, `GET /health` → `{"ok": true, "ready": …}`. For non-Python clients (issue #780 names Hermes). |

The thin client resolves the endpoint in this order: explicit `--socket` /
`--tcp-port`, then `MEMSEARCH_SERVE_SOCKET` / `MEMSEARCH_SERVE_TCP_PORT`, then
the platform default above.

## Automatic CLI routing

When a daemon is alive for the configured collection, these commands talk to it
instead of opening the store themselves:

`search` · `stats` · `expand` · `index`

That is what makes the existing **platform plugins work unchanged** —
`plugins/claude-code`, `plugins/codex`, `plugins/dsh`, `plugins/openclaw` and
`plugins/opencode` all shell out to the `memsearch` CLI, so they inherit the
warm daemon for free.

Routing is refused (local path, exactly as before) when:

* the command carries a store/provider override (`--milvus-uri`, `--model`,
  `--base-url`, `--reranker-model`, …) — the daemon owns one store and one
  model configuration, and honouring a different one through it would answer
  from the wrong place;
* `--no-daemon` is passed, or `MEMSEARCH_NO_DAEMON=1` is set.

If the daemon dies between the liveness probe and the call, the command exits
**2** with a message naming the endpoint — it never silently falls back to an
empty local result.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Ordinary error |
| `2` | Daemon down / unreachable (`EXIT_DAEMON_DOWN`) |
| `3` | `serve start` refused because another daemon already holds the pidfile (`EXIT_ALREADY_RUNNING`) |

## Protocol

JSON-lines over the socket: one JSON object per line, UTF-8 forced on Windows
(the console code page would corrupt CJK payloads). Requests carry
`{"id", "method", "params"}`; replies carry `{"id", "result"}` or
`{"id", "error": {"code", "message"}}`. Methods: `ping`, `status`, `search`,
`index`, `stats`, `expand`, `embed`, `rerank`, `watch_subscribe`, `shutdown`.
`watch_subscribe` turns its connection into an event stream (`event` lines with
the subscription id).

## Limitations

* `index` routed through the daemon does not update the CLI's local
  index-state file (`memsearch index-state` reports the last locally-run
  index).
* `memsearch mcp --daemon` routes the MCP server's store-backed tools through
  the daemon; without the flag the MCP server opens the store directly, so
  run either variant or the daemon against one collection — not both.
* Loopback only, no authentication — do not expose the port.
* No OS service registration (systemd/launchd/Windows service); start it from
  your supervisor of choice.
