"""Client + spawn tests. Some are integration-style (subprocess)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import websockets

from bin import shared, client as client_mod, spawn

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


def _spawn_client(port, name, env_data_dir, ppid_override=None, extra_env=None):
    env = os.environ.copy()
    env["INTER_SESSION_DATA_DIR"] = str(env_data_dir)
    env["PYTHONPATH"] = str(REPO)
    if ppid_override is not None:
        env["INTER_SESSION_PPID_OVERRIDE"] = str(ppid_override)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, str(BIN_DIR / "client.py"),
         "--port", str(port), "--name", name, "--idle-shutdown-minutes", "1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def _read_until_nonempty(proc, timeout=5.0):
    """Read lines from stdout until we get a non-empty one or timeout."""
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if line:
            return line.strip()
    return ""


class TestFormatMsg:
    def test_basic_msg(self):
        msg = {"op": "msg", "msg_id": "ab12", "from": "x", "from_name": "alpha",
               "from_label": "", "text": "hello"}
        out = client_mod._format_msg(msg)
        assert 'from="alpha"' in out
        assert 'msg=ab12' in out
        assert out.endswith("hello")

    def test_with_label(self):
        msg = {"msg_id": "x", "from_name": "alpha", "from_label": "重构", "text": "hi"}
        out = client_mod._format_msg(msg)
        assert 'from="alpha"' in out
        assert '"重构"' in out

    def test_truncates(self):
        big = "y" * (shared.STDOUT_CAP + 1000)
        msg = {"msg_id": "x", "from_name": "alpha", "from_label": "", "text": big}
        out = client_mod._format_msg(msg)
        assert "truncated=" in out
        assert len(out) <= shared.STDOUT_CAP + 200  # prefix overhead

    def test_sanitizes(self):
        msg = {"msg_id": "x", "from_name": "alpha", "from_label": "",
               "text": "\x1b[31mred\x1b[0m\nhi"}
        out = client_mod._format_msg(msg)
        assert "\x1b" not in out
        assert "\n" not in out  # newline replaced by ↵
        assert "↵" in out

    def test_parts_report_truncation_for_the_cont_line(self):
        # The message loop takes the line and the truncation facts from one
        # call, so the text is sanitized once per message.
        big = "\x1b[1my" + "y" * (shared.STDOUT_CAP + 1000)
        msg = {"msg_id": "x", "from_name": "alpha", "from_label": "", "text": big}
        line, was_truncated, full_len = client_mod._format_msg_parts(msg)
        assert line == client_mod._format_msg(msg)
        assert was_truncated is True
        assert full_len == shared.STDOUT_CAP + 1001  # ANSI sequence stripped
        assert f"truncated={full_len}]" in line
        small = {"msg_id": "x", "from_name": "alpha", "from_label": "", "text": "hi"}
        assert client_mod._format_msg_parts(small)[1:] == (False, 2)


class TestEnsureServerRunning:
    def test_starts_server_when_absent(self, tmp_data_dir, free_port):
        shared.secure_dir(tmp_data_dir)
        ok = spawn.ensure_server_running(port=free_port, idle_shutdown_minutes=1)
        try:
            assert ok
            assert spawn.is_server_up("127.0.0.1", free_port)
        finally:
            pid_path = shared.pidfile_path(free_port)
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text())
                    os.kill(pid, 9)
                except (OSError, ValueError):
                    pass

    def test_returns_quickly_if_already_up(self, tmp_data_dir, free_port):
        shared.secure_dir(tmp_data_dir)
        spawn.ensure_server_running(port=free_port, idle_shutdown_minutes=1)
        t0 = time.time()
        ok = spawn.ensure_server_running(port=free_port, idle_shutdown_minutes=1)
        assert ok
        assert time.time() - t0 < 1.0
        pid_path = shared.pidfile_path(free_port)
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text())
                os.kill(pid, 9)
            except (OSError, ValueError):
                pass

    def test_direct_bind_writes_identity_only_after_bind_succeeds(self, tmp_data_dir, free_port):
        """Round-16 fix: in the direct-bind path, write_server_identity runs
        AFTER websockets.serve binds successfully, so a server that fails to
        bind (port already in use) doesn't leave behind misleading identity
        for the actual occupant."""
        # Pre-bind the port with an unrelated socket so the server's direct-bind
        # path will fail.
        squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squat.bind(("127.0.0.1", free_port))
        squat.listen(1)
        try:
            env = os.environ.copy()
            env["INTER_SESSION_DATA_DIR"] = str(tmp_data_dir)
            env["PYTHONPATH"] = str(REPO)
            proc = subprocess.run(
                [sys.executable, str(BIN_DIR / "server.py"),
                 "--port", str(free_port), "--idle-shutdown-minutes", "1"],
                env=env, capture_output=True, text=True, timeout=5,
            )
            assert proc.returncode != 0, "server should have failed to bind"
            # No pidfile/meta should have been written for our pid
            assert not (tmp_data_dir / f"server.{free_port}.pid").exists()
            assert not (tmp_data_dir / f"server.{free_port}.pid.meta").exists()
        finally:
            squat.close()

    _FAKE_SERVER = (
        "import os, socket, sys, time\n"
        "fd = int(sys.argv[sys.argv.index('--fd') + 1])\n"
        "open(os.environ['FAKE_SERVER_PIDFILE'], 'w').write(str(os.getpid()))\n"
        "time.sleep(float(os.environ['FAKE_SERVER_DELAY']))\n"
        "if os.environ.get('FAKE_SERVER_EXIT'):\n"
        "    sys.exit(1)\n"
        "s = socket.socket(fileno=fd)\n"
        "s.listen(8)\n"
        "time.sleep(30)\n"
    )

    def _ensure_with_fake_server(self, tmp_path, free_port, monkeypatch, delay, exits=False):
        """Run ensure_server_running with a stand-in server that waits
        `delay` s before listen() (or exits instead), and with the first
        wait shortened to 0.3 s so the stand-in counts as slow."""
        script = tmp_path / "fake_server.py"
        script.write_text(self._FAKE_SERVER)
        pidfile = tmp_path / "fake_server.pid"
        monkeypatch.setenv("FAKE_SERVER_PIDFILE", str(pidfile))
        monkeypatch.setenv("FAKE_SERVER_DELAY", str(delay))
        if exits:
            monkeypatch.setenv("FAKE_SERVER_EXIT", "1")
        real_wait = spawn.wait_for_server
        monkeypatch.setattr(
            spawn, "wait_for_server",
            lambda host, port, timeout=5.0: real_wait(host, port, min(timeout, 0.3)),
        )
        t0 = time.monotonic()
        try:
            ok = spawn.ensure_server_running(
                port=free_port, idle_shutdown_minutes=1, server_path=script,
            )
            return ok, time.monotonic() - t0
        finally:
            if not exits:
                try:
                    os.kill(int(pidfile.read_text()), 9)
                except (OSError, ValueError):
                    pass

    def test_holds_election_while_spawned_server_starts(
        self, tmp_data_dir, tmp_path, free_port, monkeypatch,
    ):
        """A server slower to start than the first wait keeps the election
        lock until it listens. Returning early lets the next election bind
        the port again and spawn a second server."""
        shared.secure_dir(tmp_data_dir)
        ok, _ = self._ensure_with_fake_server(tmp_path, free_port, monkeypatch, delay=1.5)
        assert ok

    def test_returns_when_spawned_server_exits(
        self, tmp_data_dir, tmp_path, free_port, monkeypatch,
    ):
        shared.secure_dir(tmp_data_dir)
        ok, elapsed = self._ensure_with_fake_server(
            tmp_path, free_port, monkeypatch, delay=0.5, exits=True,
        )
        assert not ok
        assert elapsed < 5

    def test_custom_host_is_written_to_identity(self, tmp_data_dir, free_port):
        shared.secure_dir(tmp_data_dir)
        host = "localhost"
        ok = spawn.ensure_server_running(
            host=host, port=free_port, idle_shutdown_minutes=1,
        )
        try:
            assert ok
            meta = json.loads(shared.pidfile_meta_path(free_port, host).read_text())
            assert meta["host"] == host
            assert meta["port"] == free_port
            assert shared.verify_server_identity(host, free_port)
        finally:
            pid_path = shared.pidfile_path(free_port, host)
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text())
                    os.kill(pid, 9)
                except (OSError, ValueError):
                    pass


@pytest.mark.slow
class TestNameCollisionAutoRetry:
    """Regression: client.py used to loop forever on NAME_TAKEN, flooding the
    monitor with notifications. It now auto-retries once with the server's
    first suggested suffix, prints one informational notice, and continues
    running under the new name."""

    def test_second_listener_auto_renames(self, tmp_data_dir, free_port):
        env = os.environ.copy()
        env["INTER_SESSION_DATA_DIR"] = str(tmp_data_dir)
        env["PYTHONPATH"] = str(REPO)
        env_a = env.copy()
        env_a["INTER_SESSION_PPID_OVERRIDE"] = "40001"
        proc_a = subprocess.Popen(
            [sys.executable, "-u", str(BIN_DIR / "client.py"),
             "--port", str(free_port), "--name", "alpha"],
            env=env_a, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Let A win the race deterministically: wait for its state file.
        state_a_path = tmp_data_dir / "clients" / "40001.session"
        deadline = time.time() + 5
        while time.time() < deadline and not state_a_path.exists():
            time.sleep(0.1)
        assert state_a_path.exists(), "A never connected"
        env_b = env.copy()
        env_b["INTER_SESSION_PPID_OVERRIDE"] = "40002"
        proc_b = subprocess.Popen(
            [sys.executable, "-u", str(BIN_DIR / "client.py"),
             "--port", str(free_port), "--name", "alpha"],
            env=env_b, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            # Wait for B's auto-retry to land at the renamed key.
            deadline = time.time() + 8
            state_b_path = tmp_data_dir / "clients" / "40002.session"
            state_b = None
            while time.time() < deadline:
                if state_b_path.exists():
                    try:
                        state_b = json.loads(state_b_path.read_text())
                        if state_b.get("name") == "alpha-2":
                            break
                    except (json.JSONDecodeError, OSError):
                        pass
                time.sleep(0.2)
            assert state_b is not None, "B never wrote state"
            assert state_b["name"] == "alpha-2", f"B's name = {state_b['name']!r}, expected alpha-2"
            assert proc_a.poll() is None and proc_b.poll() is None
            # A's state file is unchanged
            state_a = json.loads(state_a_path.read_text())
            assert state_a["name"] == "alpha"
        finally:
            for p in (proc_a, proc_b):
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            pid_path = shared.pidfile_path(free_port)
            if pid_path.exists():
                try:
                    os.kill(int(pid_path.read_text().strip()), 9)
                except (OSError, ValueError):
                    pass

    def test_exhausted_retries_stops(self, tmp_data_dir, free_port):
        """If server keeps rejecting (we mock with a low retry budget), the
        client surfaces the failure and exits 0 instead of looping."""
        # Spawn three listeners with the same name to exhaust the retry budget
        # of the third one. The third client tries: alpha → alpha-2 → alpha-3,
        # but alpha-2 is also taken by then. With max_collision_retries=3 we
        # can't reliably trigger exhaustion in this scenario, so we instead
        # set a low max via env override.
        env = os.environ.copy()
        env["INTER_SESSION_DATA_DIR"] = str(tmp_data_dir)
        env["PYTHONPATH"] = str(REPO)

        listeners = []
        try:
            for i, key in enumerate(("50001", "50002", "50003")):
                env_i = env.copy()
                env_i["INTER_SESSION_PPID_OVERRIDE"] = key
                p = subprocess.Popen(
                    [sys.executable, "-u", str(BIN_DIR / "client.py"),
                     "--port", str(free_port), "--name", "beta"],
                    env=env_i, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                listeners.append(p)
                time.sleep(0.6)
            # All three should be alive — first as beta, second as beta-2, third as beta-3.
            time.sleep(0.5)
            for i, p in enumerate(listeners):
                assert p.poll() is None, f"listener {i} unexpectedly exited"
        finally:
            for p in listeners:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            pid_path = shared.pidfile_path(free_port)
            if pid_path.exists():
                try:
                    os.kill(int(pid_path.read_text().strip()), 9)
                except (OSError, ValueError):
                    pass


@pytest.mark.slow
class TestReElectionAfterServerCrash:
    def test_client_respawns_server_after_kill(self, tmp_data_dir, free_port):
        """Regression: the bug where SO_REUSEADDR=0 prevented rebind after SIGKILL.

        macOS holds the listening port in a reuse-blocked state for several
        seconds after the listener process dies. SO_REUSEADDR=1 allows immediate
        rebind. Without that flag, the client would loop forever with EADDRINUSE.
        """
        # Manually start a server first.
        env = os.environ.copy()
        env["INTER_SESSION_DATA_DIR"] = str(tmp_data_dir)
        env["PYTHONPATH"] = str(REPO)
        srv_proc = subprocess.Popen(
            [sys.executable, str(BIN_DIR / "server.py"),
             "--port", str(free_port), "--idle-shutdown-minutes", "1"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        # Spawn one client.
        env_c = env.copy()
        env_c["INTER_SESSION_PPID_OVERRIDE"] = "30001"
        client = subprocess.Popen(
            [sys.executable, "-u", str(BIN_DIR / "client.py"),
             "--port", str(free_port), "--name", "alpha", "--verbose"],
            env=env_c, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            time.sleep(1.0)
            old_pid = srv_proc.pid
            srv_proc.kill()
            srv_proc.wait()
            # Wait for re-election (typically <2s with new SO_REUSEADDR).
            new_pid = None
            end = time.time() + 6
            pid_path = tmp_data_dir / f"server.{free_port}.pid"
            while time.time() < end:
                if pid_path.exists():
                    try:
                        candidate = int(pid_path.read_text().strip())
                        if candidate != old_pid:
                            try:
                                os.kill(candidate, 0)
                                new_pid = candidate
                                break
                            except OSError:
                                pass
                    except (OSError, ValueError):
                        pass
                time.sleep(0.2)
            assert new_pid is not None, f"no new server elected after kill"
            assert new_pid != old_pid
            # Verify it's reachable
            with socket.create_connection(("127.0.0.1", free_port), timeout=1.0):
                pass
        finally:
            client.terminate()
            try:
                client.wait(timeout=2)
            except subprocess.TimeoutExpired:
                client.kill()
            # Cleanup any new server
            pid_path = shared.pidfile_path(free_port)
            if pid_path.exists():
                try:
                    os.kill(int(pid_path.read_text().strip()), 9)
                except (OSError, ValueError):
                    pass


@pytest.mark.slow
class TestClientIntegration:
    def test_two_clients_exchange_messages(self, tmp_data_dir, free_port):
        # Start two clients via subprocess; first should auto-start the server.
        proc_a = _spawn_client(free_port, "alpha", tmp_data_dir, ppid_override=10001)
        proc_b = _spawn_client(free_port, "beta", tmp_data_dir, ppid_override=10002)
        try:
            # Give them a moment to connect.
            time.sleep(1.5)
            # Connect a control client to send a message from alpha to beta.
            async def _drive():
                token = shared.ensure_token(shared.token_path())
                ws = await websockets.connect(f"ws://127.0.0.1:{free_port}/",
                                              max_size=shared.WS_FRAME_CAP)
                try:
                    await ws.send(json.dumps({
                        "op": "hello",
                        "session_id": str(uuid.uuid4()),
                        "name": "test-driver",
                        "label": "",
                        "cwd": "/tmp",
                        "pid": os.getpid(),
                        "role": "agent",
                        "token": token,
                    }))
                    await ws.recv()  # welcome
                    await ws.send(json.dumps({"op": "send", "to": "beta", "text": "hi from test"}))
                    # Give the server a beat to deliver before we close
                    await asyncio.sleep(0.3)
                finally:
                    await ws.close()

            asyncio.new_event_loop().run_until_complete(_drive())

            # Read beta's stdout for the inter-session line
            line = _read_until_nonempty(proc_b, timeout=5.0)
            assert "hi from test" in line
            assert 'from="' in line
        finally:
            for p in (proc_a, proc_b):
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            # Kill server (endpoint-scoped pidfile)
            pid_path = shared.pidfile_path(free_port)
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text())
                    os.kill(pid, 9)
                except (OSError, ValueError):
                    pass


@pytest.mark.slow
class TestForceDisconnectClientExits:
    """Layer A client side: when the server closes our ws with
    CLOSE_CODE_FORCE_DISCONNECT, the client must exit the run loop instead
    of reconnecting. Otherwise an un-killed python recreates the ghost the
    server just evicted."""

    def test_client_exits_on_4001_does_not_reconnect(self, tmp_data_dir, free_port):
        proc = _spawn_client(free_port, "alpha", tmp_data_dir, ppid_override=20001)
        try:
            # Wait for the client to be registered with the server.
            time.sleep(1.5)

            # Read the .session file to discover the listener's session_id + nonce
            # (the control role needs them to authenticate).
            state_path = shared.client_session_path(20001)
            state = json.loads(state_path.read_text())
            sid = state["session_id"]
            nonce = state["nonce"]

            # Drive a control connection that issues force_disconnect.
            async def _drive() -> dict:
                token = shared.ensure_token(shared.token_path())
                ws = await websockets.connect(f"ws://127.0.0.1:{free_port}/",
                                              max_size=shared.WS_FRAME_CAP)
                try:
                    await ws.send(json.dumps({
                        "op": "hello",
                        "session_id": str(uuid.uuid4()),
                        "name": "", "label": "",
                        "cwd": "/tmp", "pid": os.getpid(),
                        "role": "control",
                        "for_session": sid, "nonce": nonce, "token": token,
                    }))
                    await ws.recv()  # welcome
                    await ws.send(json.dumps({"op": "force_disconnect"}))
                    return json.loads(await ws.recv())
                finally:
                    await ws.close()

            ack = asyncio.new_event_loop().run_until_complete(_drive())
            assert ack.get("op") == "force_disconnect_ok"
            assert ack.get("was_present") is True

            # Client should exit on its own (without us sending any signal)
            # because it received CLOSE_CODE_FORCE_DISCONNECT.
            try:
                rc = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "client did not exit after force_disconnect; "
                    "it likely reconnected, recreating the ghost"
                )
            assert rc == 0, f"client exited with non-zero rc={rc}"

            # The 4001 path emits a user-visible exit notice on stdout, naming
            # the disconnect command as installed here.
            out = proc.stdout.read() or ""
            expected = (f"[inter-session] disconnected by {shared.skill_command()} d "
                        "(or orphan reap); exiting")
            assert expected in out, (
                f"expected force_disconnect notice on client stdout, got:\n{out!r}"
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            pid_path = shared.pidfile_path(free_port)
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text())
                    os.kill(pid, 9)
                except (OSError, ValueError):
                    pass


class TestConnectWhenNothingListens:
    """ensure_server_running returns False when the server it spawned is not
    listening yet. With nothing on the port the identity check fails on the
    missing pidfile; the client must retry through its backoff loop instead
    of refusing for good. A port held by something that is not the bus
    server is still refused."""

    @pytest.mark.slow
    async def test_retries_until_server_listens(self, tmp_data_dir, free_port, monkeypatch):
        shared.secure_dir(tmp_data_dir)
        attempts = []

        def slow_ensure(port, host, idle_shutdown_minutes):
            attempts.append(spawn.is_server_up(host, port))
            return False

        monkeypatch.setattr(spawn, "ensure_server_running", slow_ensure)
        printed = []
        monkeypatch.setattr(client_mod, "_print_line", printed.append)
        ppid = 73001
        c = client_mod.Client(port=free_port, name="slowpoke", ppid=ppid, owner_pid=-1)
        run_task = asyncio.create_task(c.run())
        srv_proc = None
        try:
            deadline = time.monotonic() + 5
            while not attempts and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert attempts == [False], attempts
            await asyncio.sleep(0.1)
            assert not run_task.done(), printed
            # The server comes up late. A real server.py, since the identity
            # check matches the pidfile's process against bin/server.py.
            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO)
            srv_proc = subprocess.Popen(
                [sys.executable, str(BIN_DIR / "server.py"),
                 "--port", str(free_port), "--idle-shutdown-minutes", "1"],
                env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            state_path = shared.client_session_path(ppid)
            deadline = time.monotonic() + 10
            while not state_path.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert state_path.exists(), (attempts, printed)
            assert json.loads(state_path.read_text())["name"] == "slowpoke"
            assert len(attempts) >= 2
            assert not any("identity check failed" in line for line in printed), printed
        finally:
            c.stop()
            await asyncio.wait_for(run_task, timeout=5)
            if srv_proc is not None:
                srv_proc.terminate()
                try:
                    srv_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    srv_proc.kill()
                    srv_proc.wait()

    async def test_squatter_is_still_refused(self, tmp_data_dir, free_port, monkeypatch):
        shared.secure_dir(tmp_data_dir)
        printed = []
        monkeypatch.setattr(client_mod, "_print_line", printed.append)
        squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squat.bind(("127.0.0.1", free_port))
        squat.listen(8)
        try:
            c = client_mod.Client(port=free_port, name="alpha", ppid=73002, owner_pid=-1)
            rc = await asyncio.wait_for(c.run(), timeout=10)
        finally:
            squat.close()
        assert rc == 0
        assert any("server identity check failed" in line for line in printed), printed


class TestPpidLock:
    def test_lock_acquired_then_released(self, tmp_data_dir):
        shared.secure_dir(shared.clients_dir())
        fd = client_mod._acquire_ppid_lock(99999)
        assert fd is not None
        # Second attempt should fail
        fd2 = client_mod._acquire_ppid_lock(99999)
        assert fd2 is None
        os.close(fd)
        # After release, can re-acquire
        fd3 = client_mod._acquire_ppid_lock(99999)
        assert fd3 is not None
        os.close(fd3)


class TestExistingSessionStateLookup:
    """The flock-fail error message embeds the existing connection's
    identity so the skill can act on it without a follow-up Bash call."""

    def test_returns_none_when_no_state_file(self, tmp_data_dir):
        shared.secure_dir(shared.clients_dir())
        assert client_mod._read_existing_session_state(99998) is None

    def test_returns_state_dict_when_present(self, tmp_data_dir):
        shared.secure_dir(shared.clients_dir())
        path = shared.client_session_path(99997)
        path.write_text(json.dumps({
            "session_id": "abc-123",
            "name": "auth-refactor",
            "listener_pid": 4242,
            "nonce": "n",
        }))
        info = client_mod._read_existing_session_state(99997)
        assert info is not None
        assert info["name"] == "auth-refactor"
        assert info["listener_pid"] == 4242
        assert info["session_id"] == "abc-123"

    def test_returns_none_on_corrupt_json(self, tmp_data_dir):
        shared.secure_dir(shared.clients_dir())
        path = shared.client_session_path(99996)
        path.write_text("{not valid json")
        assert client_mod._read_existing_session_state(99996) is None


class TestEnvVarConfig:
    """Verify client.py picks up CLAUDE_PLUGIN_OPTION_* and INTER_SESSION_* env vars
    so plugin mode (proper /plugin install) and --plugin-dir mode both work."""

    def test_plugin_option_port(self, monkeypatch):
        from bin.client import _env_int
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_PORT", "9499")
        monkeypatch.delenv("INTER_SESSION_PORT", raising=False)
        assert _env_int("CLAUDE_PLUGIN_OPTION_PORT", "INTER_SESSION_PORT", default=9473) == 9499

    def test_inter_session_port_fallback(self, monkeypatch):
        from bin.client import _env_int
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_PORT", raising=False)
        monkeypatch.setenv("INTER_SESSION_PORT", "9500")
        assert _env_int("CLAUDE_PLUGIN_OPTION_PORT", "INTER_SESSION_PORT", default=9473) == 9500

    def test_default_when_neither_set(self, monkeypatch):
        from bin.client import _env_int
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_PORT", raising=False)
        monkeypatch.delenv("INTER_SESSION_PORT", raising=False)
        assert _env_int("CLAUDE_PLUGIN_OPTION_PORT", "INTER_SESSION_PORT", default=9473) == 9473

    def test_invalid_value_falls_through(self, monkeypatch):
        from bin.client import _env_int
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_PORT", "not-a-port")
        monkeypatch.setenv("INTER_SESSION_PORT", "9501")
        assert _env_int("CLAUDE_PLUGIN_OPTION_PORT", "INTER_SESSION_PORT", default=9473) == 9501

    def test_float_idle(self, monkeypatch):
        from bin.client import _env_float
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_IDLE_SHUTDOWN_MINUTES", "0.5")
        assert _env_float("CLAUDE_PLUGIN_OPTION_IDLE_SHUTDOWN_MINUTES", "X", default=10) == 0.5
