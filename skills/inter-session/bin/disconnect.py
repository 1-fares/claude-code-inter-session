"""Disconnect this session from the inter-session bus.

Implements the bus-side and OS-side half of `/is d`. The skill is expected
to also `TaskStop` the Monitor task for harness-side cleanup (cheap, and
idempotent if the python has already exited on its own).

Order of operations:
  1. Read the listener's `.session` file via `discover.find_listener_state_with_path`.
     If no state is found, print "not connected" and exit 0.
  2. Open an authenticated control connection (role=control, nonce from
     `.session`) and send `force_disconnect`. The server pops the listener
     from its registry, broadcasts `peer_left`, and closes the listener's
     ws with `CLOSE_CODE_FORCE_DISCONNECT` (the listener's reconnect loop
     honors that code by exiting). The name is freed bus-side immediately.
  3. Verify via psutil that the listener pid is actually gone. If still
     alive, tuple-verify identity (psutil cmdline contains the absolute
     path to this skill's `client.py`) and escalate SIGTERM → SIGKILL,
     re-verifying identity before each signal so a recycled pid is not
     mis-targeted.
  4. ~200 ms barrier so an immediate `/is c <samename>` doesn't race
     server-side `_unregister`.
  5. Print one of:
        not connected
        not connected (stale state cleaned up)
        disconnected
        disconnected (required SIGTERM after force_disconnect)
        disconnected (required SIGKILL -- pid <N> was unresponsive)
        error: <message>

The script never touches a process whose cmdline does not look like our
client.py — same-UID PID recycling is contained that way.
"""

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
import time
import uuid

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import websockets

from bin import shared, discover

# Absolute path of this skill's client.py. Used to verify that the pid we're
# about to signal is actually running OUR client.py (and not some unrelated
# python process that happens to have the same pid after recycling). We
# `.resolve()` so a symlinked install matches.
_CLIENT_PATH = (Path(__file__).resolve().parent / "client.py").resolve()


def _proc_is_our_client(pid: int, expected_listener_pid: int) -> bool:
    """psutil-based identity check.

    Returns True iff the live process at `pid` has our `client.py` in its
    argv AND its pid matches what `.session` reported. False on any error,
    on missing process, on cmdline mismatch.
    """
    if pid <= 0 or pid != expected_listener_pid:
        return False
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        cmd = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:
        return False
    for arg in cmd:
        if not arg:
            continue
        try:
            if Path(arg).resolve() == _CLIENT_PATH:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:
        return shared.safe_pid_alive(pid)
    try:
        proc = psutil.Process(pid)
        st = proc.status()
        return st not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:
        return False


def _wait_for_pid_exit(pid: int, deadline_s: float) -> bool:
    """Poll until `pid` is no longer alive or deadline expires.
    Returns True if the pid exited within the deadline."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


async def _send_force_disconnect(state: dict, state_path) -> tuple[str, bool]:
    """Open a control connection and send force_disconnect.

    Returns (outcome, name_freed) where outcome is one of:
      "ack"            — server acked; `name_freed` indicates was_present
      "stale"          — server returned unknown_peer/unauthorized; .session
                         pruned where safe
      "no_server"      — could not reach the server (identity check failed
                         or connect refused). Caller should still try the
                         OS-level kill path.
      "error: <msg>"   — unexpected server-side error; details for the user
    """
    host = state.get("host", "127.0.0.1")
    port = state.get("port", shared.DEFAULT_PORT)
    token = state.get("token")
    for_session = state.get("session_id")
    nonce = state.get("nonce")
    if not (token and for_session and nonce):
        return "stale", False

    if not shared.verify_server_identity(host, port):
        return "no_server", False
    try:
        ws = await websockets.connect(
            f"ws://{host}:{port}/", max_size=shared.WS_FRAME_CAP,
        )
    except OSError:
        return "no_server", False

    try:
        await ws.send(json.dumps({
            "op": "hello",
            "session_id": str(uuid.uuid4()),
            "name": "", "label": "",
            "cwd": os.getcwd(), "pid": os.getpid(),
            "role": shared.Role.CONTROL.value,
            "for_session": for_session, "nonce": nonce, "token": token,
        }))
        try:
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        except asyncio.TimeoutError:
            return "error: no welcome from server", False
        if welcome.get("op") == "error":
            code = welcome.get("code", "")
            if code in (shared.ErrorCode.UNKNOWN_PEER, shared.ErrorCode.UNAUTHORIZED):
                discover.unlink_if_matches(state_path, state)
                return "stale", False
            return f"error: hello rejected: {code}", False

        await ws.send(json.dumps({"op": "force_disconnect"}))
        try:
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        except asyncio.TimeoutError:
            return "error: no force_disconnect ack from server", False
        if ack.get("op") == "force_disconnect_ok":
            return "ack", bool(ack.get("was_present", False))
        if ack.get("op") == "error":
            return f"error: {ack.get('code', '')} {ack.get('message', '')}", False
        return f"error: unexpected ack: {ack!r}", False
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def _reap_local_listener(listener_pid: int) -> str:
    """Verify the listener pid is gone; escalate SIGTERM → SIGKILL if not.
    Returns a short tag for the final printable line."""
    if listener_pid <= 0:
        return "ok"
    # Give the listener a grace window to exit on its own (it will exit
    # after receiving CLOSE_CODE_FORCE_DISCONNECT from the server).
    if _wait_for_pid_exit(listener_pid, deadline_s=1.0):
        return "ok"
    # Still alive — verify identity before sending any signal.
    if not _proc_is_our_client(listener_pid, listener_pid):
        return "ok"  # not our client anymore (recycled or gone); leave it alone
    try:
        os.kill(listener_pid, 15)  # SIGTERM
    except ProcessLookupError:
        return "ok"
    except OSError:
        return "ok"
    if _wait_for_pid_exit(listener_pid, deadline_s=2.0):
        return "sigterm"
    # Still alive after SIGTERM — re-verify identity, then SIGKILL.
    if not _proc_is_our_client(listener_pid, listener_pid):
        return "sigterm"
    try:
        os.kill(listener_pid, 9)  # SIGKILL
    except ProcessLookupError:
        return "sigterm"
    except OSError:
        return "sigterm"
    if _wait_for_pid_exit(listener_pid, deadline_s=2.0):
        return f"sigkill:{listener_pid}"
    return f"unkillable:{listener_pid}"


async def _run(args) -> int:
    state, state_path = discover.find_listener_state_with_path()
    if state is None:
        print("not connected")
        return 0

    listener_pid = int(state.get("listener_pid", 0))
    outcome, _was_present = await _send_force_disconnect(state, state_path)

    if outcome == "stale":
        # Server didn't know about this listener; the .session was orphaned.
        # If the underlying python is still running, try to reap it.
        reap_tag = _reap_local_listener(listener_pid)
        if reap_tag in ("sigterm", "ok"):
            print("not connected (stale state cleaned up)")
        elif reap_tag.startswith("sigkill"):
            pid = reap_tag.split(":", 1)[1]
            print(f"disconnected (required SIGKILL -- pid {pid} was unresponsive). "
                  f"Worth reporting if recurring.")
        else:
            pid = reap_tag.split(":", 1)[1]
            print(f"error: pid {pid} is unresponsive to SIGTERM and SIGKILL",
                  file=sys.stderr)
            return 1
        return 0

    if outcome == "no_server":
        # Server unreachable. If the python is still up, kill it locally so
        # the user actually ends up disconnected.
        if listener_pid > 0 and _pid_alive(listener_pid):
            reap_tag = _reap_local_listener(listener_pid)
            if reap_tag.startswith("sigkill"):
                pid = reap_tag.split(":", 1)[1]
                print(f"disconnected (server unreachable; required SIGKILL -- "
                      f"pid {pid} was unresponsive). Worth reporting if recurring.")
            elif reap_tag == "sigterm":
                print("disconnected (server unreachable; required SIGTERM)")
            elif reap_tag == "ok":
                print("disconnected (server unreachable; client exited)")
            else:
                pid = reap_tag.split(":", 1)[1]
                print(f"error: pid {pid} is unresponsive to SIGTERM and SIGKILL",
                      file=sys.stderr)
                return 1
        else:
            print("not connected (server unreachable; client not running)")
        return 0

    if outcome.startswith("error:"):
        print(outcome, file=sys.stderr)
        return 1

    # outcome == "ack": server acked force_disconnect. The name is free
    # bus-side. The python should exit on its own on CLOSE_CODE_FORCE_DISCONNECT
    # within ~1s; if not, escalate.
    reap_tag = _reap_local_listener(listener_pid)
    # Brief barrier so a follow-up /is c <samename> doesn't race the
    # server's last bits of _unregister bookkeeping.
    time.sleep(0.2)
    if reap_tag == "ok":
        print("disconnected")
    elif reap_tag == "sigterm":
        print("disconnected (required SIGTERM after force_disconnect)")
    elif reap_tag.startswith("sigkill"):
        pid = reap_tag.split(":", 1)[1]
        print(f"disconnected (required SIGKILL -- pid {pid} was unresponsive). "
              f"Worth reporting if recurring.")
    else:
        pid = reap_tag.split(":", 1)[1]
        print(f"error: pid {pid} is unresponsive to SIGTERM and SIGKILL",
              file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Disconnect this CC session from the inter-session bus.",
    )
    parser.parse_args()
    return asyncio.run(_run(None))


if __name__ == "__main__":
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("dependencies missing -- run /inter-session install-deps",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
