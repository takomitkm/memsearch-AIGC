"""Re-apply this repository's two local patches into an installed memsearch.

Two separate things get clobbered by ordinary package management:

1. ``@zilliz/memsearch-dsh`` in a DSH profile's ``node_modules`` — any
   ``dsh plugin add``/``remove`` rewrites it from the registry tarball.
2. ``memsearch`` in a ``uv tool`` environment — ``uv tool upgrade``/reinstall
   rewrites it from the PyPI wheel.

Both patches live in this repository, so this script reads its masters straight
from the working tree and writes them over the installed copies. Nothing is
written unless every master matches the file it is about to replace, and each
replaced file is copied aside first.

Usage
-----
    python tools/windows/patch_installs.py check
    python tools/windows/patch_installs.py apply
    python tools/windows/patch_installs.py --plugin-dir ... --env-dir ... apply

``check`` never writes and exits 1 when something needs patching. ``apply`` is
the mode to pass when you do want the write.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Baseline hash of the unpatched published plugin file. A target matching this
# means the patch was overwritten by a reinstall; anything else means the file
# was edited by hand, which is reported but still repaired from the repository.
UPSTREAM_PLUGIN_INDEX_JS = "b7007701149f528a0c63092f4660f0eac630c10b5b2ecde3fbfcf3118b963149"

PLUGIN_FILES = ["index.js", "README.md"]
PLUGIN_SOURCE = REPO / "plugins" / "dsh"
PLUGIN_TARGET_DEFAULT = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "@zilliz" / "memsearch-dsh"

# Every CLI file that differs from the PyPI wheel: fork-touched plus fork-only
# (the MCP server, expand and the knowledge store ship only in this fork, so
# leaving them off this list made `uv tool upgrade` silently drop them).
PY_FILES = [
    "cli.py",
    "config.py",
    "core.py",
    "reranker.py",
    "remote_reranker.py",
    "expand.py",
    "knowledge_store.py",
    "mcp_server.py",
]
PY_SOURCE = REPO / "src" / "memsearch"


def uv_tools_root() -> Path:
    """Where ``uv tool install`` keeps its environments, if uv can be asked."""
    try:
        out = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, errors="replace", timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path(os.environ.get("UV_TOOL_DIR", str(Path.home() / "AppData" / "Roaming" / "uv" / "tools")))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def uv_site_packages(tool_root: Path) -> Path:
    """Locate the ``memsearch`` package inside the uv tool environment.

    The layout differs by platform (``Lib/site-packages`` on Windows,
    ``lib/pythonX.Y/site-packages`` elsewhere), so the environment's own
    interpreter is asked where it keeps packages.
    """
    for exe in (tool_root / "Scripts" / "python.exe", tool_root / "bin" / "python"):
        if not exe.is_file():
            continue
        out = subprocess.run(
            [str(exe), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
        purelib = out.stdout.strip()
        if purelib:
            return Path(purelib) / "memsearch"
    raise SystemExit(f"cannot find a python interpreter under {tool_root}; pass --site-dir")


def inspect(names: list[str], source: Path, target: Path, label: str) -> tuple[list, bool]:
    """Return (repairs, fatal). ``fatal`` means nothing may be written."""
    print(f"\n[{label}]")
    print(f"  master {source}")
    print(f"  live   {target}")
    if not target.is_dir():
        print("  [abort] live directory is absent - this layer is not installed; nothing written.")
        return [], True
    repairs: list[tuple[str, Path, Path, str]] = []
    for name in names:
        master = source / name
        dest = target / name
        if not master.is_file():
            print(f"  [abort] master missing: {master} - nothing written.")
            return [], True
        ms = sha256(master)
        ts = sha256(dest) if dest.is_file() else None
        if ts == ms:
            print(f"  {name:<20} patched   {ms[:16]}...")
        elif ts is None:
            print(f"  {name:<20} absent    must be created ({ms[:16]}...)")
            repairs.append((name, master, dest, "(new)"))
        else:
            why = "overwritten by the published file" if ts == UPSTREAM_PLUGIN_INDEX_JS else "differs from master"
            print(f"  {name:<20} {why}")
            print(f"      current {ts[:16]}... ({dest.stat().st_size} bytes)")
            print(f"      master  {ms[:16]}... ({master.stat().st_size} bytes)")
            repairs.append((name, master, dest, ts))
    return repairs, False


def write_repairs(repairs: list, backup: Path, label: str) -> bool:
    if not repairs:
        print(f"  {label}: already patched, nothing written.")
        return True
    keep = backup / time.strftime("%Y%m%d-%H%M%S")
    keep.mkdir(parents=True, exist_ok=True)
    print(f"  {label}: writing {len(repairs)} file(s); previous copies kept in {keep}")
    for name, master, dest, cur in repairs:
        if cur != "(new)":
            shutil.copy2(dest, keep / f"{name}.replaced-{cur[:12]}")
        shutil.copy2(master, dest)
        os.utime(dest, None)
        new = sha256(dest)
        if new != sha256(master):
            print(f"  [fail] {name} does not match master after writing: {new[:16]}...")
            print(f"         the previous copy is still in {keep}")
            return False
        print(f"    {name:<20} verified after write  {new[:16]}...")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["check", "apply"], default="check")
    ap.add_argument(
        "--plugin-dir", type=Path, default=PLUGIN_TARGET_DEFAULT, help="installed @zilliz/memsearch-dsh directory"
    )
    ap.add_argument(
        "--env-dir", type=Path, default=None, help="uv tool environment directory holding the memsearch venv"
    )
    ap.add_argument("--site-dir", type=Path, default=None, help="installed memsearch package directory")
    ap.add_argument("--backup-dir", type=Path, default=REPO / ".patch-backups", help="where replaced files are kept")
    args = ap.parse_args()

    print(f"repository {REPO}")
    print(f"mode       {args.mode}")

    plugin_repairs, fatal = inspect(PLUGIN_FILES, PLUGIN_SOURCE, args.plugin_dir, "dsh plugin / node_modules")
    py_repairs: list = []
    if not fatal:
        try:
            tool_root = args.env_dir or (uv_tools_root() / "memsearch")
            site = args.site_dir or uv_site_packages(tool_root)
        except SystemExit as exc:
            print(f"\n[abort] cannot locate the memsearch CLI layer: {exc} - nothing written.")
            return 1
        py_repairs, fatal = inspect(PY_FILES, PY_SOURCE, site, "memsearch CLI / site-packages")

    if fatal:
        return 1
    if args.mode == "check":
        needed = len(plugin_repairs) + len(py_repairs)
        if needed:
            print(f"\n{needed} file(s) need patching; check mode wrote nothing.")
            return 1
        print("\nboth layers are patched.")
        return 0

    ok = write_repairs(plugin_repairs, args.backup_dir, "dsh plugin") and write_repairs(
        py_repairs, args.backup_dir, "memsearch CLI"
    )
    if not ok:
        return 1
    print("\nThe plugin layer needs a full DSH exit and restart; the CLI layer needs a fresh terminal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
