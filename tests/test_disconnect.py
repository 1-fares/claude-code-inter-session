"""Tests for the `/is d` helper script (bin/disconnect.py).

The helper performs both halves of disconnection:
  - bus-side: control-role connection + force_disconnect (Layer A)
  - OS-side : verify pid is gone; escalate SIGTERM / SIGKILL with identity
              re-verify if not.

The end-to-end scenarios here are subprocess-based and marked slow.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bin import shared

REPO = Path(__file__).resolve().parent.parent
BIN_DIR = REPO / "skills" / "inter-session" / "bin"
DISCONNECT = BIN_DIR / "disconnect.py"
CLIENT = BIN_DIR / "client.py"


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


def _disconnect_env(data_dir: Path, ppid_override: int) -> dict:
    env = os.environ.copy()
    env["INTER_SESSION_DATA_DIR"] = str(data_dir)
    env["INTER_SESSION_PPID_OVERRIDE"] = str(ppid_override)
    env["PYTHONPATH"] = str(REPO)
    return env


def _spawn_client(port: int, name: str, data_dir: Path, ppid_override: int):
    env = _disconnect_env(data_dir, ppid_override)
    return subprocess.Popen(
        [sys.executable, str(CLIENT),
         "--port", str(port), "--name", name, "--idle-shutdown-minutes", "1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def _run_disconnect(data_dir: Path, ppid_override: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DISCONNECT)],
        capture_output=True, text=True, timeout=15,
        env=_disconnect_env(data_dir, ppid_override),
    )


def _kill_server(port: int):
    pid_path = shared.pidfile_path(port)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text())
            os.kill(pid, 9)
        except (OSError, ValueError):
            pass


class TestDisconnectNotConnected:
    def test_no_session_prints_not_connected(self, tmp_data_dir):
        # No client ever ran; .session file does not exist.
        result = _run_disconnect(tmp_data_dir, ppid_override=30001)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "not connected"

    def test_idempotent_second_call(self, tmp_data_dir):
        # Two consecutive calls with no client both print "not connected".
        for _ in range(2):
            result = _run_disconnect(tmp_data_dir, ppid_override=30002)
            assert result.returncode == 0
            assert result.stdout.strip() == "not connected"


@pytest.mark.slow
class TestDisconnectHappyPath:
    def test_disconnects_a_running_client(self, tmp_data_dir, free_port):
        proc = _spawn_client(free_port, "alpha", tmp_data_dir, ppid_override=31001)
        try:
            # Give the client time to connect, write .session, and register.
            time.sleep(1.5)
            assert proc.poll() is None, "client died before disconnect"

            result = _run_disconnect(tmp_data_dir, ppid_override=31001)
            assert result.returncode == 0, (
                f"disconnect.py failed: stdout={result.stdout!r} "
                f"stderr={result.stderr!r}"
            )
            assert result.stdout.strip() == "disconnected", (
                f"unexpected output: {result.stdout!r}"
            )

            # The python should have exited on 4001 within ~1s.
            try:
                rc = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "client did not exit after force_disconnect; the 4001 "
                    "handler did not break the reconnect loop"
                )
            assert rc == 0
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            _kill_server(free_port)


@pytest.mark.slow
class TestDisconnectStaleState:
    def test_stale_session_file_with_no_listener(self, tmp_data_dir, free_port):
        """A .session file remains from a prior run but neither the python
        nor the server registration exists. The helper should connect to
        the server, hit unknown_peer/unauthorized, clean up the file, and
        print 'not connected (stale state cleaned up)'.
        """
        # Start a real server (without registering as our listener) so the
        # control connection has somewhere to land.
        proc = _spawn_client(free_port, "decoy", tmp_data_dir, ppid_override=32101)
        try:
            time.sleep(1.5)
            assert proc.poll() is None

            # Write a stale .session under a different ppid so the helper
            # discovers it via the direct-path lookup, not the decoy's path.
            ppid = 32001
            shared.secure_dir(shared.clients_dir())
            stale_path = shared.client_session_path(ppid)
            stale_state = {
                "session_id": str(uuid.uuid4()),
                "name": "ghost",
                "label": "",
                "token": shared.ensure_token(shared.token_path()),
                "nonce": "stale-nonce",
                "listener_pid": 1,  # init — alive but definitely not our client
                "host": "127.0.0.1",
                "port": free_port,
                "created_at": "2024-01-01T00:00:00+00:00",
            }
            stale_path.write_text(json.dumps(stale_state))

            result = _run_disconnect(tmp_data_dir, ppid_override=ppid)
            assert result.returncode == 0, (
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
            assert "stale state cleaned up" in result.stdout, result.stdout
            assert not stale_path.exists(), \
                "stale .session should have been removed"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            _kill_server(free_port)
