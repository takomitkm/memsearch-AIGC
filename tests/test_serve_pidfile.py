"""Pidfile acquire/release/stale-takeover, with liveness faked for determinism.

``is_pid_alive`` is monkeypatched throughout so the tests do not depend on real
process ids surviving (and never touch kernel32), while ``acquire``'s file
mechanics — atomic create, stale unlink, ownership-checked release — run for
real against tmp_path.
"""

import os

import pytest

from memsearch.serve import pidfile
from memsearch.serve.pidfile import DaemonAlreadyRunningError, acquire, read_pid


@pytest.fixture
def pid_path(tmp_path):
    return tmp_path / "serve.sock.pid"


def test_acquire_writes_own_pid(pid_path):
    handle = acquire(pid_path, pid=4242)
    assert handle.pid == 4242
    assert read_pid(pid_path) == 4242
    handle.release()
    assert not pid_path.exists()


def test_acquire_creates_parent_dirs(tmp_path):
    nested = tmp_path / "memsearch-home" / "serve.pid"
    handle = acquire(nested, pid=1)
    assert nested.is_file()
    handle.release()


def test_acquire_refuses_live_owner(pid_path, monkeypatch):
    monkeypatch.setattr(pidfile, "is_pid_alive", lambda pid: True)
    pid_path.write_text("9999", encoding="utf-8")
    with pytest.raises(DaemonAlreadyRunningError) as exc:
        acquire(pid_path, pid=1234)
    assert exc.value.pid == 9999
    # The live owner's file is left untouched.
    assert read_pid(pid_path) == 9999


def test_acquire_takes_over_dead_owner(pid_path, monkeypatch):
    monkeypatch.setattr(pidfile, "is_pid_alive", lambda pid: pid == 5555)  # 5555 alive, others dead
    pid_path.write_text("777", encoding="utf-8")  # stale, pid 777 dead
    handle = acquire(pid_path, pid=7777)
    assert read_pid(pid_path) == 7777
    handle.release()


def test_release_only_removes_own_record(pid_path, monkeypatch):
    monkeypatch.setattr(pidfile, "is_pid_alive", lambda pid: False)
    handle = acquire(pid_path, pid=111)
    # Another process overwrote the pidfile (a takeover after a crash):
    pid_path.write_text("222", encoding="utf-8")
    handle.release()
    assert read_pid(pid_path) == 222  # the newer daemon's file survives


def test_read_pid_handles_missing_and_garbage(tmp_path):
    missing = tmp_path / "nope.pid"
    assert read_pid(missing) is None
    junk = tmp_path / "junk.pid"
    junk.write_text("not-a-pid", encoding="utf-8")
    assert read_pid(junk) is None


def test_is_pid_alive_rejects_nonpositive():
    assert pidfile.is_pid_alive(0) is False
    assert pidfile.is_pid_alive(-1) is False


def test_is_pid_alive_of_self():
    assert pidfile.is_pid_alive(os.getpid()) is True


def test_default_socket_and_pid_keyed_by_collection(monkeypatch, tmp_path):
    monkeypatch.setattr(pidfile.Path, "expanduser", lambda self: tmp_path)
    sock, pid = pidfile.default_socket_and_pid("my coll")
    assert sock.name == "serve-my_coll.sock"
    assert pid.name == "serve-my_coll.pid"
