"""send.py --file: the file pointer must reach the receiver whole.

The receiving client prints at most shared.STDOUT_CAP characters of a
message body. send.py places the pointer after the note, so when note and
pointer together exceed the cap the clip used to fall on the path. These
tests pin the composed text against the cap and check, through a real
server and client, that the printed line carries the complete path.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

from bin import shared, send as send_mod
from tests.test_helpers import (
    _kill_server,
    _run_helper,
    _spawn_listener,
    _wait_for_state,
)

CAP = shared.STDOUT_CAP
BARE_OVERHEAD = len("read and execute the task in the file at ")


def _note_form(text: str, path: Path) -> str:
    return f"{text} (read the file at {path} and act on it)"


def _bare_form(path: Path) -> str:
    return f"read and execute the task in the file at {path}"


def _file_with_path_len(base: Path, total_len: int) -> Path:
    """Create a regular file whose resolved absolute path is exactly
    total_len characters long, under base."""
    base = base.resolve()
    rest = total_len - len(str(base))  # characters after base, incl. "/"
    assert rest >= 2, f"base {base} too long for a {total_len}-char path"
    d = base
    while rest > 202:
        d = d / ("d" * 200)
        rest -= 201
    d.mkdir(parents=True, exist_ok=True)
    f = d / ("f" * (rest - 1))
    f.write_text("task body\n")
    assert len(str(f.resolve())) == total_len
    return f.resolve()


def _receiver_body(composed: str) -> str:
    """What the receiving client prints of the message body."""
    return shared.truncate_for_stdout(shared.sanitize_for_stdout(composed))[0]


class TestFittingMessageUnchanged:
    def test_note_and_pointer_under_cap(self, tmp_path):
        f = _file_with_path_len(tmp_path, len(str(tmp_path.resolve())) + 20)
        assert send_mod._compose_text("done: unit 3", str(f)) == \
            _note_form("done: unit 3", f)

    def test_note_and_pointer_exactly_at_cap(self, tmp_path):
        f = _file_with_path_len(tmp_path, 150)
        note = "n" * (CAP - len(_note_form("", f)))
        out = send_mod._compose_text(note, str(f))
        assert out == _note_form(note, f)
        assert len(out) == CAP

    def test_short_note_with_long_path_kept_whole(self, tmp_path, capsys):
        # 7 + 35 + 350 = 392: fits, so the note is neither clipped nor dropped.
        f = _file_with_path_len(tmp_path, 350)
        out = send_mod._compose_text("done: x", str(f))
        assert out == _note_form("done: x", f)
        assert capsys.readouterr().err == ""

    def test_bare_pointer_exactly_at_cap(self, tmp_path):
        f = _file_with_path_len(tmp_path, CAP - BARE_OVERHEAD)
        out = send_mod._compose_text(None, str(f))
        assert out == _bare_form(f)
        assert len(out) == CAP


class TestPointerKeptWithinCap:
    def test_long_note_is_clipped_and_path_survives(self, tmp_path, capsys):
        f = _file_with_path_len(tmp_path, 150)
        note = "status: " + "s" * 292  # 300 chars
        out = send_mod._compose_text(note, str(f))
        assert len(out) <= CAP
        suffix = f" (read the file at {f} and act on it)"
        assert out.endswith(suffix)
        assert out.startswith("status: sss")
        assert out[: -len(suffix)].endswith("...")
        assert suffix in _receiver_body(out)
        err = capsys.readouterr().err
        assert "clipped" in err and str(CAP) in err

    def test_note_dropped_when_no_room_left(self, tmp_path, capsys):
        # 300 + 35 + 350 > 400 and the path leaves under the minimum room
        # for a note; the bare pointer (41 + 350 = 391) still fits.
        f = _file_with_path_len(tmp_path, 350)
        out = send_mod._compose_text("x" * 300, str(f))
        assert out == _bare_form(f)
        assert len(out) <= CAP
        assert str(f) in _receiver_body(out)
        assert "dropped" in capsys.readouterr().err

    def test_pointer_over_cap_refused_with_note(self, tmp_path):
        f = _file_with_path_len(tmp_path, 380)
        with pytest.raises(ValueError, match="shorter path"):
            send_mod._compose_text("done: x", str(f))

    def test_pointer_over_cap_refused_without_note(self, tmp_path):
        f = _file_with_path_len(tmp_path, CAP - BARE_OVERHEAD + 1)
        with pytest.raises(ValueError, match="shorter path"):
            send_mod._compose_text(None, str(f))

    def test_cli_refuses_over_cap_pointer(self, tmp_data_dir, tmp_path):
        # No listener: before the cap check existed, send.py got as far as
        # "not connected". The refusal must come first, with a non-zero exit.
        f = _file_with_path_len(tmp_path, 380)
        r = _run_helper("send.py", tmp_data_dir, 99998,
                        "--to", "beta", "--text", "done: x", "--file", str(f))
        assert r.returncode != 0
        assert "shorter path" in r.stderr, f"stderr={r.stderr!r}"
        assert "sent ->" not in r.stdout


def _read_msg_line(proc: subprocess.Popen, timeout: float = 10.0) -> str | None:
    """Return the first `[inter-session msg=` line the client prints, or
    None after timeout. A thread does the blocking reads so the test never
    hangs on a silent pipe."""
    q: queue.Queue = queue.Queue()

    def pump():
        for line in proc.stdout:
            q.put(line)
            if line.startswith("[inter-session msg="):
                return
        q.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = q.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:
            return None
        if line.startswith("[inter-session msg="):
            return line


@pytest.mark.slow
class TestPointerEndToEnd:
    def _send_and_receive(self, tmp_data_dir, free_port, ppids, *send_args):
        ppid_a, ppid_b = ppids
        listener_a = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid_a)
        listener_b = _spawn_listener(free_port, "beta", tmp_data_dir, ppid_b)
        try:
            assert _wait_for_state(tmp_data_dir, ppid_a) is not None
            assert _wait_for_state(tmp_data_dir, ppid_b) is not None
            time.sleep(0.5)
            r = _run_helper("send.py", tmp_data_dir, ppid_a,
                            "--to", "beta", *send_args)
            assert r.returncode == 0, f"stderr={r.stderr!r}"
            line = _read_msg_line(listener_b)
            assert line is not None, "receiver printed no message line"
            return r, line
        finally:
            for p in (listener_a, listener_b):
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            _kill_server()

    def test_long_summary_and_long_path_arrive_with_full_path(
            self, tmp_data_dir, free_port, tmp_path):
        base_len = len(str(tmp_path.resolve()))
        path_len = max(150, base_len + 40)
        assert path_len <= 300
        f = _file_with_path_len(tmp_path, path_len)
        summary = "done: " + "w" * 294  # 300 chars
        r, line = self._send_and_receive(
            tmp_data_dir, free_port, (20101, 20102),
            "--text", summary, "--file", str(f))
        assert f"(read the file at {f} and act on it)" in line, line
        assert "truncated=" not in line
        assert r.stdout.startswith("sent -> beta (")

    def test_fitting_pointer_arrives_byte_for_byte(
            self, tmp_data_dir, free_port, tmp_path):
        f = _file_with_path_len(tmp_path, len(str(tmp_path.resolve())) + 20)
        r, line = self._send_and_receive(
            tmp_data_dir, free_port, (20111, 20112),
            "--text", "status: short note", "--file", str(f))
        expected = _note_form("status: short note", f)
        body = line.rstrip("\n").split("] ", 1)[1]
        assert body == expected
        assert r.stdout == f"sent -> beta ({len(expected)} chars)\n"
