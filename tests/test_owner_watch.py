"""Tests for the owner-session liveness watch in client.py.

Without this watch, closing the WSL/terminal window leaves the python
listener running indefinitely (bash wrapper launched under
start_new_session=True survives the tty SIGHUP). The watch polls the
CC ancestor pid and exits the monitor when that pid disappears.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bin import shared, client as client_mod

REPO = Path(__file__).resolve().parent.parent
BIN_DIR = REPO / "skills" / "inter-session" / "bin"


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    d = tmp_path / "inter-session"
    monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(d))
    return d


@pytest.fixture
def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_client_with_owner(port, name, data_dir, ppid_override, owner_pid,
                             interval_s=0.2):
    env = os.environ.copy()
    env["INTER_SESSION_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(REPO)
    env["INTER_SESSION_PPID_OVERRIDE"] = str(ppid_override)
    env["INTER_SESSION_OWNER_PID"] = str(owner_pid)
    env["INTER_SESSION_OWNER_CHECK_INTERVAL_S"] = str(interval_s)
    return subprocess.Popen(
        [sys.executable, str(BIN_DIR / "client.py"),
         "--port", str(port), "--name", name, "--idle-shutdown-minutes", "1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def _kill_server(port: int):
    pid_path = shared.pidfile_path(port)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text())
            os.kill(pid, 9)
        except (OSError, ValueError):
            pass


class TestOwnerPidResolution:
    """Without an explicit owner_pid, Client falls back to
    find_cc_ancestor_pid(). In tests there's no `claude` ancestor, so the
    fallback returns -1 and the watch should be disabled."""

    def test_owner_pid_disabled_outside_cc_environment(self):
        # In the pytest process there is no `claude` ancestor.
        c = client_mod.Client(name="x")
        # find_cc_ancestor_pid returns -1 outside CC, which disables the
        # watch (the run() loop checks owner_pid > 0).
        assert c.owner_pid <= 0 or c.owner_pid != os.getpid()

    def test_explicit_owner_pid_used(self):
        c = client_mod.Client(name="x", owner_pid=12345)
        assert c.owner_pid == 12345

    def test_explicit_owner_pid_zero_disables(self):
        # Passing 0 means "no owner to watch" — same as -1.
        c = client_mod.Client(name="x", owner_pid=0)
        assert c.owner_pid == 0


@pytest.mark.slow
class TestOwnerWatchEndToEnd:
    def test_client_exits_when_owner_pid_disappears(self, tmp_data_dir, free_port):
        # Spawn a throwaway "owner" process whose pid the client will watch.
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            client = _spawn_client_with_owner(
                free_port, "alpha", tmp_data_dir,
                ppid_override=40001, owner_pid=owner.pid, interval_s=0.2,
            )
            try:
                # Let the client connect and register.
                time.sleep(1.5)
                assert client.poll() is None, (
                    "client died before owner was killed: "
                    f"stderr={client.stderr.read(500)!r}"
                )
                # Kill the owner. Client should detect within ~2 cycles (0.4s).
                owner.terminate()
                owner.wait(timeout=2)
                try:
                    rc = client.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pytest.fail(
                        "client did not exit after owner pid disappeared; "
                        "owner-watch did not trigger"
                    )
                assert rc == 0
                # User-visible exit notice on stdout.
                out = client.stdout.read() or ""
                assert "owning CC session" in out, (
                    f"expected owner-gone notice; got:\n{out!r}"
                )
            finally:
                if client.poll() is None:
                    client.terminate()
                    try:
                        client.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        client.kill()
        finally:
            if owner.poll() is None:
                owner.kill()
            _kill_server(free_port)

    def test_client_keeps_running_while_owner_alive(self, tmp_data_dir, free_port):
        """Sanity: a healthy owner pid does not cause the watch to fire."""
        # Use our own test process pid as the owner — it lives for the test.
        client = _spawn_client_with_owner(
            free_port, "alpha", tmp_data_dir,
            ppid_override=40101, owner_pid=os.getpid(), interval_s=0.1,
        )
        try:
            time.sleep(1.5)
            # Several watch cycles must have elapsed without firing.
            assert client.poll() is None, (
                "owner-watch incorrectly killed the client while the owner "
                "was alive"
            )
        finally:
            client.terminate()
            try:
                client.wait(timeout=2)
            except subprocess.TimeoutExpired:
                client.kill()
            _kill_server(free_port)
