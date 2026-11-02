"""Pidfile management for the serve daemon.

A pidfile guards against a second ``memsearch serve`` starting while one is
already live, and lets clients and ``serve stop`` discover the running daemon's
pid. Creation is atomic (``O_CREAT | O_EXCL``) so two daemons cannot both
believe they won the race.

Stale detection is the piece ``maintenance._file_lock`` does not need but a
long-lived daemon does: if a previous daemon was SIGKILLed it left a pidfile
behind pointing at a pid that is either gone or — worse — recycled to an
unrelated process. ``acquire`` therefore refuses a pidfile whose owner is still
alive, and takes over one whose recorded pid is dead, so a crashed daemon never
locks the operator out of restarting.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when the pidfile is held by a live process.

    ``pid`` is the surviving daemon's process id, so callers can report which
    instance is in the way or send it a shutdown request.
    """

    def __init__(self, path: Path, pid: int) -> None:
        super().__init__(f"serve daemon already running (pid {pid}) at {path}")
        self.path = path
        self.pid = pid


def read_pid(path: Path) -> int | None:
    """Return the pid recorded in ``path``, or None if absent or unreadable."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    """Report whether ``pid`` currently names a running process.

    POSIX uses ``kill(pid, 0)``, which touches no signal but raises
    ``ProcessLookupError`` for a dead pid and ``PermissionError`` for a live one
    owned by someone else — the latter counts as alive. On Windows ``os.kill``
    has unrelated signal semantics, so liveness is decided with
    ``OpenProcess`` plus ``GetExitCodeProcess`` to exclude recycled and zombie
    handles. The platform-specific import stays inside the function so importing
    this module never touches kernel32 on non-Windows hosts.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return _windows_pid_alive(pid)


def _windows_pid_alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # A denied handle means the pid exists but belongs to another user or is
        # a protected process; treat it as alive rather than clobber its pidfile.
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class PidHandle:
    """A held pidfile: its path and the pid it recorded.

    Acquire once at daemon start via :func:`acquire`, then :meth:`release` on
    shutdown (``finally`` in the daemon loop). Releasing removes the file only
    when it still records this handle's pid, so a takeover after a crash cannot
    delete a newer daemon's pidfile.
    """

    def __init__(self, path: Path, pid: int) -> None:
        self.path = path
        self.pid = pid

    def release(self) -> None:
        if read_pid(self.path) == self.pid:
            with contextlib.suppress(OSError):
                self.path.unlink()


def acquire(path: Path, *, pid: int | None = None) -> PidHandle:
    """Claim ``path`` for a new daemon, or raise if a live one already holds it.

    The pid is written inside the freshly created file, so a reader never sees
    an empty pidfile. A pre-existing file is checked for liveness first: a live
    owner raises :class:`DaemonAlreadyRunningError`, a dead one (crashed daemon)
    is removed and the claim retried once. ``O_CREAT | O_EXCL`` still arbitrates
    the retry against any racing starter, so exactly one caller wins.
    """
    owner_pid = pid if pid is not None else os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)

    recorded = read_pid(path)
    if recorded is not None and is_pid_alive(recorded):
        raise DaemonAlreadyRunningError(path, recorded)
    if recorded is not None and not is_pid_alive(recorded):
        with contextlib.suppress(OSError):
            path.unlink()

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        recorded = read_pid(path)
        if recorded is not None and is_pid_alive(recorded):
            raise DaemonAlreadyRunningError(path, recorded) from exc
        raise DaemonAlreadyRunningError(path, recorded or owner_pid) from exc
    try:
        os.write(fd, str(owner_pid).encode())
    finally:
        os.close(fd)
    return PidHandle(path, owner_pid)


def default_socket_and_pid(collection: str) -> tuple[Path, Path]:
    """Return the (socket, pidfile) paths for ``collection`` under ``~/.memsearch``.

    The daemon owns one collection's Milvus Lite file lock, so its endpoints are
    keyed by collection name: two collections get two daemons, two clients, and
    no contention — without the operator threading sockets through by hand.
    """
    base = Path("~/.memsearch").expanduser()
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in collection)
    return base / f"serve-{slug}.sock", base / f"serve-{slug}.pid"
