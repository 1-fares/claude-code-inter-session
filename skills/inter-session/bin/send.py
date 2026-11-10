"""Send a single message to one session, then exit. role=control."""

from __future__ import annotations

# Bootstrap: re-exec under the project's isolated venv if it exists.
import os
import sys
from pathlib import Path
_VENV = Path.home() / ".claude" / "data" / "inter-session" / "venv"
_VENV_PY = _VENV / "bin" / "python"
if (not os.environ.get("INTER_SESSION_NO_REEXEC")
        and _VENV_PY.is_file()
        and Path(sys.prefix).resolve() != _VENV.resolve()):
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

import argparse
import asyncio
import json
import uuid

# Allow running as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import shared

# Runs after the venv re-exec above. Without websockets, print the
# documented notice on stderr (stdout is reserved for the success line)
# and exit 1 instead of a traceback.
shared.require_runtime_deps(sys.stderr, exit_code=1)

import websockets

from bin import discover


# A note shorter than this, after clipping, carries little beyond its
# ellipsis; below it the note is dropped and the bare pointer is sent.
_MIN_NOTE_ROOM = 20


def _compose_text(text, file) -> str:
    """Build the message body to send.

    With --file, send a short pointer instructing the receiver to read the
    file directly (via its own Read tool) rather than streaming the body
    through the bus. The path is resolved to an absolute path and must exist.

    The receiver prints only the first shared.STDOUT_CAP characters of a
    body, and the pointer follows the note, so an over-cap message loses the
    end of the path while the rest still looks like a complete path. The
    note keeps its place at the front (receivers read a leading `done:`,
    `status:` or `question:` as the kind of message), and the cap is spent
    on the pointer instead: a message that fits is sent unchanged; otherwise
    the note is clipped to the room the pointer leaves, or dropped when that
    room is under _MIN_NOTE_ROOM. Clipping and dropping are reported on
    stderr. A pointer that does not fit on its own raises ValueError: no
    composition can deliver that path intact."""
    if file:
        abspath = Path(file).expanduser().resolve(strict=True)  # raises if missing
        if not abspath.is_file():
            raise ValueError(f"not a regular file: {abspath}")
        cap = shared.STDOUT_CAP
        bare = f"read and execute the task in the file at {abspath}"
        if text:
            suffix = f" (read the file at {abspath} and act on it)"
            if len(text) + len(suffix) <= cap:
                return f"{text}{suffix}"
            room = cap - len(suffix)
            if room >= _MIN_NOTE_ROOM:
                print(f"note clipped from {len(text)} to {room} chars so the file "
                      f"pointer fits the {cap}-char notification cap",
                      file=sys.stderr)
                return f"{text[:room - 3]}...{suffix}"
            if len(bare) <= cap:
                print(f"note dropped: the file pointer takes {len(suffix)} of the "
                      f"{cap}-char notification cap; sending the pointer alone",
                      file=sys.stderr)
                return bare
        if len(bare) > cap:
            raise ValueError(
                f"the file pointer would be {len(bare)} chars, over the "
                f"{cap}-char notification cap, and the receiver would get a "
                f"clipped path; use a file at a shorter path "
                f"({abspath} is {len(str(abspath))} chars)")
        return bare
    return text


# Upper bound in seconds on waiting for the outcome of a send or broadcast.
# The server reports a failed delivery before it answers the `ping` that
# follows, and knows the outcome of a direct message within twice its send
# timeout (at most one bound waiting in the recipient's queue, at most one
# for the write), so the wait has to outlast that; a success is answered at
# once. shared.send_timeout_s() reads the same setting as the server. No
# answer at all within the wait is reported as an error: a bus that does not
# answer cannot have confirmed the delivery.
_OUTCOME_WAIT_S = 2 * shared.send_timeout_s() + 1.0

# Upper bound in seconds on the bus's answer to the hello. A bus that took
# the connection and then said nothing held the sender without end.
_WELCOME_WAIT_S = 5.0


async def _await_outcome(ws):
    """The send's `error` frame, or None once the send went through.

    The server answers a successful send or broadcast with nothing, so every
    success used to wait out the whole timed wait. The server handles one
    connection's frames in order and writes a send's error before it reads
    the next frame, so a `ping` sent right behind the send is answered only
    after the send was handled: `pong` first means sent, an `error` first is
    the send's. The server answers `ping` with `pong` on every connection,
    without a check that could fail. Other frames are skipped. When the bus
    sends neither frame within the bound, a `no_answer` error is returned;
    when it closes the connection, a `connection_closed` error."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _OUTCOME_WAIT_S
    try:
        await ws.send(json.dumps({"op": "ping"}))
        while True:
            left = deadline - loop.time()
            if left <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("op") == "pong":
                return None
            if frame.get("op") == "error":
                return frame
    except asyncio.TimeoutError:
        return {"op": "error", "code": "no_answer",
                "message": f"no answer from the bus within "
                           f"{_OUTCOME_WAIT_S:g}s; delivery unknown"}
    except websockets.ConnectionClosed:
        return {"op": "error", "code": "connection_closed",
                "message": "the bus closed the connection; delivery unknown"}


async def _run(args) -> int:
    state, state_path = discover.find_listener_state_with_path()
    if state is None:
        print(f"not connected; run {shared.skill_command()} in this Claude Code session first",
              file=sys.stderr)
        return 1

    host = state.get("host", "127.0.0.1")
    port = state.get("port", shared.DEFAULT_PORT)
    token = state["token"]
    for_session = state["session_id"]
    nonce = state["nonce"]

    if not shared.verify_server_identity(host, port):
        print(f"server identity check failed ({host}:{port} not held by bin/server.py)",
              file=sys.stderr)
        return 1

    try:
        ws = await websockets.connect(
            f"ws://{host}:{port}/", max_size=shared.WS_FRAME_CAP,
            close_timeout=2,
        )
    except OSError as e:
        print(f"connect failed: {e}", file=sys.stderr)
        return 1

    try:
        await ws.send(json.dumps({
            "op": "hello",
            "session_id": str(uuid.uuid4()),
            "name": "",
            "label": "",
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "role": shared.Role.CONTROL.value,
            "for_session": for_session,
            "nonce": nonce,
            "token": token,
        }))
        welcome_raw = await asyncio.wait_for(ws.recv(), timeout=_WELCOME_WAIT_S)
        welcome = json.loads(welcome_raw)
        if welcome.get("op") == "error":
            code = welcome.get("code", "")
            if code in (shared.ErrorCode.UNKNOWN_PEER, shared.ErrorCode.UNAUTHORIZED):
                # Stale state file: listener referenced no longer registered.
                # TOCTOU-safe: only unlink if the file still has the same
                # session_id+nonce as we read; otherwise a fresh listener has
                # written new state between our read and now.
                discover.unlink_if_matches(state_path, state)
                print(f"not connected; run {shared.skill_command()} in this Claude Code session first",
                      file=sys.stderr)
                return 1
            print(f"hello error: {code} {welcome.get('message', '')}",
                  file=sys.stderr)
            return 1

        op = "broadcast" if args.all else "send"
        payload = {"op": op, "text": args.text}
        if not args.all:
            payload["to"] = args.to
        await ws.send(json.dumps(payload))

        resp = await _await_outcome(ws)
        if resp is not None:
            msg = f"error: {resp.get('code', '')}: {resp.get('message', '')}"
            if "matches" in resp:
                msg += f" (matches: {', '.join(resp['matches'])})"
            if "candidates" in resp:
                msg += f" (try: {', '.join(resp['candidates'])})"
            print(msg, file=sys.stderr)
            return 1
    except websockets.ConnectionClosed:
        # Closed during the hello, the welcome or the send itself; a close
        # after the send is reported by _await_outcome.
        print("error: connection_closed: the bus closed the connection "
              "before the message was confirmed", file=sys.stderr)
        return 1
    except asyncio.TimeoutError:
        print(f"error: no_answer: no welcome from the bus within "
              f"{_WELCOME_WAIT_S:g}s; nothing sent", file=sys.stderr)
        return 1
    finally:
        await ws.close()
    # Emit an explicit success line on stdout. Previously a successful send
    # produced no output ("success is silence"), which is indistinguishable
    # from a dropped/empty tool result, so callers retried and re-verified
    # successful sends, a major source of wasted cycles. With this line, empty
    # stdout unambiguously means failure.
    target = "all" if args.all else args.to
    print(f"sent -> {target} ({len(args.text)} chars)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", help="target name or short session_id (omit with --all)")
    parser.add_argument("--all", action="store_true", help="broadcast to all other sessions")
    parser.add_argument("--text", help="message text (required unless --file)")
    parser.add_argument("--file", help="send a pointer to a file for the receiver to read and execute")
    args = parser.parse_args()
    if not args.all and not args.to:
        parser.error("--to required unless --all")
    if not args.text and not args.file:
        parser.error("one of --text or --file is required")
    try:
        args.text = _compose_text(args.text, args.file)
    except (OSError, ValueError) as e:
        parser.error(f"--file: {e}")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
