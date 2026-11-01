# Keeping the local patches applied

Two installed copies of this project carry patches that ordinary package
management overwrites:

| Layer | Live location | What clobbers it |
|-------|---------------|------------------|
| DSH plugin | `~/.dsh/profiles/<profile>/node_modules/@zilliz/memsearch-dsh` | any `dsh plugin add` / `dsh plugin remove`, which re-extracts the registry tarball |
| memsearch CLI | the `uv tool` environment for `memsearch` (`.../site-packages/memsearch`) | `uv tool upgrade memsearch`, `uv tool install`, or `uv cache` recreation |

Neither layer reads the patch from this repository at runtime, so an overwrite
is silent: the plugin reverts to the version that cannot find the `dsh` CLI on
Windows (see the *Windows: launching the headless summarizer* section of
[`plugins/dsh/README.md`](../plugins/dsh/README.md)), and the CLI reverts to a
build where `reranker.model = "remote:…"` silently falls through to the local
ONNX/PyTorch backends and is skipped when neither is installed.

`patch_installs.py` takes its masters from this working tree — `plugins/dsh/`
and `src/memsearch/` — so updating the repository is what updates the patch.

```bash
# read-only: what is in place, what got overwritten (exit 1 if anything is stale)
python tools/windows/patch_installs.py check

# write the repository files over the installed copies, keeping the replaced ones
python tools/windows/patch_installs.py apply
```

`apply` refuses to write anything if a master is missing, and copies each file
it replaces into `.patch-backups/<timestamp>/` before overwriting. After
`apply`, restart is required and it differs per layer: the plugin code is
already loaded in the DSH host process, so fully quit DSH (not just close the
window) and reopen it; the CLI is re-read per invocation, so just open a new
terminal.

Non-default install locations are handled with `--plugin-dir`, `--env-dir`, or
`--site-dir`; `check` prints the paths it resolved first, so a wrong guess is
visible before anything is written.
