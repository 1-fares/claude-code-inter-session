"""send.py learns the outcome of a send from the server instead of a timed wait.

The server answers a successful send or broadcast with nothing. send.py
used to wait 1.0 s for an error frame, so every successful send cost at
least a second. It now sends a `ping` right behind the message: the server
handles one connection's frames in order, so the send's error, if any,
arrives before the `pong`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import select
import subprocess
import sys
import time

import pytest
import websockets

from bin import send as send_mod
from bin.server import Server
from tests.test_helpers import REPO, _kill_server, _spawn_listener, _wait_for_state

# A success is answered at once; with the outcome wait raised to OUTCOME_WAIT_S,
# a send that returns well under it was answered, not timed out. The margin
# leaves room for a loaded host.
OUTCOME_WAIT_S = 30.0
ANSWERED_S = 10.0


class _FakeWs:
    """Hands out `frames` one per recv(), then never answers. An exception
    in `frames` is raised instead of returned."""

    def __init__(self, frames):
        self.frames = [json.dumps(f) if isinstance(f, dict) else f for f in frames]
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.frames:
            frame = self.frames.pop(0)
            if isinstance(frame, BaseException):
                raise frame
            return frame
        await asyncio.sleep(3600)


def _outcome(frames, monkeypatch, wait_s=0.3):
    monkeypatch.setattr(send_mod, "_OUTCOME_WAIT_S", wait_s)
    ws = _FakeWs(frames)
    t0 = time.monotonic()
    res = asyncio.run(send_mod._await_outcome(ws))
    return res, ws.sent, time.monotonic() - t0


ERR = {"op": "error", "code": "unknown_peer", "message": "no agent matches 'x'"}
UNAUTH = {"op": "error", "code": "unauthorized", "message": "listener no longer connected"}
PONG = {"op": "pong"}
CLOSED = websockets.exceptions.ConnectionClosedError(None, None)


class TestAwaitOutcome:
    def test_pong_is_success_at_once(self, monkeypatch):
        res, sent, dt = _outcome([PONG], monkeypatch, wait_s=OUTCOME_WAIT_S)
        assert res is None and sent == [{"op": "ping"}]
        assert dt < ANSWERED_S

    def test_error_before_pong_is_the_sends(self, monkeypatch):
        res, _, dt = _outcome([ERR, PONG], monkeypatch, wait_s=OUTCOME_WAIT_S)
        assert res == ERR and dt < ANSWERED_S

    def test_error_is_reported_without_waiting_for_pong(self, monkeypatch):
        res, _, dt = _outcome([ERR], monkeypatch, wait_s=OUTCOME_WAIT_S)
        assert res == ERR and dt < ANSWERED_S

    def test_unauthorized_before_pong_is_the_sends(self, monkeypatch):
        # `ping` itself is never refused, so every error ahead of the pong
        # is the send's, `unauthorized` included.
        res, _, _ = _outcome([UNAUTH, PONG], monkeypatch)
        assert res == UNAUTH

    def test_error_after_pong_is_not_the_sends(self, monkeypatch):
        res, _, _ = _outcome([PONG, ERR], monkeypatch)
        assert res is None

    def test_other_frames_are_skipped(self, monkeypatch):
        res, _, _ = _outcome([{"op": "peer_joined"}, "not json", "[1]",
                              {"op": "list_ok", "sessions": []}, ERR, PONG],
                             monkeypatch)
        assert res == ERR

    def test_no_answer_is_an_error_not_a_success(self, monkeypatch):
        res, _, dt = _outcome([], monkeypatch, wait_s=0.3)
        assert res is not None and res["code"] == "no_answer"
        assert "delivery unknown" in res["message"] and 0.25 <= dt < 2.0

    def test_closed_connection_is_an_error_not_a_success(self, monkeypatch):
        res, _, dt = _outcome([{"op": "peer_joined"}, CLOSED], monkeypatch,
                              wait_s=OUTCOME_WAIT_S)
        assert res == {"op": "error", "code": "connection_closed",
                       "message": "the bus closed the connection; delivery unknown"}
        assert dt < ANSWERED_S

    def test_wait_outlasts_twice_the_server_delivery_bound(self):
        # A direct message waits up to one bound in the recipient's queue
        # and its write takes up to one more.
        assert send_mod._OUTCOME_WAIT_S > 2 * send_mod.shared.send_timeout_s()


def test_sender_and_server_read_the_same_bound(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("INTER_SESSION_SEND_TIMEOUT_S", "12")
    assert Server(host="127.0.0.1", port=1)._send_timeout_s == 12.0
    # _OUTCOME_WAIT_S is fixed at import: read it in a fresh interpreter.
    env = dict(os.environ, PYTHONPATH=str(REPO / "skills" / "inter-session"))
    out = subprocess.run(
        [sys.executable, "-c", "from bin import send; print(send._OUTCOME_WAIT_S)"],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert float(out) == 2 * 12.0 + 1.0


class TestConnectionClosedDuringWait:
    """The bus closes the connection after the send, before it answers the
    ping: one `error:` line and exit code 1, no traceback."""

    @pytest.mark.parametrize("how", ["close", "abort"])
    async def test_one_error_line(self, how, free_port, monkeypatch, capsys):
        async def handler(ws):
            hello = json.loads(await ws.recv())
            await ws.send(json.dumps({"op": "welcome", "session_id": "c-1",
                                      "assigned_name": "alpha",
                                      "for_session": hello["for_session"]}))
            assert json.loads(await ws.recv())["op"] == "send"
            if how == "close":
                await ws.close()
            else:
                ws.transport.abort()

        server = await websockets.serve(handler, "127.0.0.1", free_port)
        state = {"host": "127.0.0.1", "port": free_port, "token": "t",
                 "session_id": "s-1", "nonce": "n-1"}
        monkeypatch.setattr(send_mod.discover, "find_listener_state_with_path",
                            lambda: (state, None))
        monkeypatch.setattr(send_mod.shared, "verify_server_identity", lambda h, p: True)
        monkeypatch.setattr(send_mod, "_OUTCOME_WAIT_S", OUTCOME_WAIT_S)
        try:
            args = argparse.Namespace(to="beta", text="hello", all=False, file=None)
            t0 = time.monotonic()
            rc = await send_mod._run(args)
            dt = time.monotonic() - t0
        finally:
            server.close()
            await server.wait_closed()
        out, err = capsys.readouterr()
        assert (rc, out) == (1, "")
        assert err == ("error: connection_closed: the bus closed the connection; "
                       "delivery unknown\n")
        assert dt < ANSWERED_S


class TestBusSilentOrGoneBeforeTheSend:
    """The bus closes, or says nothing, before the welcome: one `error:`
    line and exit code 1, no traceback and no wait without end."""

    @pytest.mark.parametrize("how,want", [
        ("close", "error: connection_closed: the bus closed the connection "
                  "before the message was confirmed\n"),
        ("mute", "error: no_answer: no welcome from the bus within 0.3s; "
                 "nothing sent\n"),
    ])
    async def test_one_error_line(self, how, want, free_port, monkeypatch, capsys):
        async def handler(ws):
            await ws.recv()
            if how == "close":
                await ws.close()
            else:
                await asyncio.sleep(30)

        server = await websockets.serve(handler, "127.0.0.1", free_port)
        state = {"host": "127.0.0.1", "port": free_port, "token": "t",
                 "session_id": "s-1", "nonce": "n-1"}
        monkeypatch.setattr(send_mod.discover, "find_listener_state_with_path",
                            lambda: (state, None))
        monkeypatch.setattr(send_mod.shared, "verify_server_identity", lambda h, p: True)
        monkeypatch.setattr(send_mod, "_WELCOME_WAIT_S", 0.3)
        try:
            args = argparse.Namespace(to="beta", text="hello", all=False, file=None)
            t0 = time.monotonic()
            rc = await send_mod._run(args)
            dt = time.monotonic() - t0
        finally:
            server.close()
            await server.wait_closed()
        out, err = capsys.readouterr()
        assert (rc, out, err) == (1, "", want)
        assert dt < ANSWERED_S


def _send(to=None, text="hello", all_=False):
    args = argparse.Namespace(to=to, text=text, all=all_, file=None)
    t0 = time.monotonic()
    rc = asyncio.run(send_mod._run(args))
    return rc, time.monotonic() - t0


def _readline(proc, timeout=10.0):
    """One line of `proc`'s stdout, or what arrived of it within `timeout`.
    Reads the pipe unbuffered, so a lost message cannot hang the run."""
    fd = proc.stdout.fileno()
    line = b""
    deadline = time.monotonic() + timeout
    while not line.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            break
        chunk = os.read(fd, 1)
        if not chunk:
            break
        line += chunk
    return line.decode(errors="replace")


@pytest.mark.slow
class TestSendAgainstServer:
    @pytest.fixture
    def bus(self, tmp_data_dir, free_port, monkeypatch):
        keys = {"alpha": 73001, "beta": 73002, "bravo": 73003}
        procs = {n: _spawn_listener(free_port, n, tmp_data_dir, k) for n, k in keys.items()}
        try:
            for k in keys.values():
                assert _wait_for_state(tmp_data_dir, k) is not None
            time.sleep(0.3)
            monkeypatch.setenv("INTER_SESSION_PPID_OVERRIDE", str(keys["alpha"]))
            monkeypatch.setattr(send_mod, "_OUTCOME_WAIT_S", OUTCOME_WAIT_S)
            yield procs
        finally:
            for p in procs.values():
                p.terminate()
            for p in procs.values():
                try:
                    p.wait(timeout=3)
                except Exception:
                    p.kill()
            _kill_server()

    def test_successful_send_is_fast(self, bus, capsys):
        rc, dt = _send(to="beta", text="fast one")
        out, err = capsys.readouterr()
        assert (rc, out, err) == (0, "sent -> beta (8 chars)\n", "")
        assert dt < ANSWERED_S, f"send took {dt:.3f}s"
        line = _readline(bus["beta"])
        assert "fast one" in line and 'from="alpha"' in line

    def test_successful_broadcast_is_fast(self, bus, capsys):
        rc, dt = _send(text="to all", all_=True)
        out, err = capsys.readouterr()
        assert (rc, out, err) == (0, "sent -> all (6 chars)\n", "")
        assert dt < ANSWERED_S, f"broadcast took {dt:.3f}s"
        assert "to all" in _readline(bus["beta"])

    def test_unknown_peer_still_fails(self, bus, capsys):
        rc, dt = _send(to="nobody")
        out, err = capsys.readouterr()
        assert (rc, out, err) == (1, "", "error: unknown_peer: no agent matches 'nobody'\n")
        assert dt < ANSWERED_S

    def test_ambiguous_prefix_lists_matches(self, bus, capsys):
        rc, _ = _send(to="b")
        out, err = capsys.readouterr()
        assert rc == 1 and out == ""
        assert err.startswith("error: ambiguous: ambiguous prefix 'b' (matches: ")
        assert "beta" in err and "bravo" in err

    def test_oversized_broadcast_still_fails(self, bus, capsys):
        rc, _ = _send(text="x" * (256 * 1024 + 1), all_=True)
        out, err = capsys.readouterr()
        assert (rc, out, err) == (1, "", "error: text_too_long: text exceeds broadcast cap\n")
