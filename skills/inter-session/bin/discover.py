"""Listener-state discovery for helper CLIs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from bin import shared


def find_listener_state(start_pid: Optional[int] = None) -> Optional[dict]:
    """Convenience wrapper returning just the state dict (no path)."""
    state, _ = find_listener_state_with_path(start_pid)
    return state


def find_listener_state_with_path(
    start_pid: Optional[int] = None,
) -> tuple[Optional[dict], Optional[Path]]:
    """Return (state, path) for the current CC session's listener, or
    (None, None) if no listener is found. The caller can use `path` to
    clean up a stale state file (e.g., when the server reports
    `unknown_peer` on hello).
    """
    key = shared.resolve_listener_key()
    direct_path = shared.client_session_path(key)
    state = _read_state(direct_path)
    if state is not None:
        return state, direct_path

    try:
        import psutil
    except ImportError:
        return _walk_via_proc_with_path(start_pid or os.getpid())

    pid = start_pid or os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        path = shared.client_session_path(pid)
        state = _read_state(path)
        if state is not None:
            return state, path
        try:
            p = psutil.Process(pid)
            pid = p.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None, None
    return None, None


def _read_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _walk_via_proc_with_path(pid: int) -> tuple[Optional[dict], Optional[Path]]:
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        path = shared.client_session_path(pid)
        state = _read_state(path)
        if state is not None:
            return state, path
        ppid = _ppid_of(pid)
        if ppid is None:
            return None, None
        pid = ppid
    return None, None


def unlink_if_matches(path: Optional[Path], expected_state: Optional[dict]) -> bool:
    """Delete `path` only if (a) its contents match `expected_state` by
    `session_id`+`nonce`, AND (b) the corresponding listener `<ppid>.lock`
    flock can be acquired (i.e. no live listener holds it).

    Race-safe vs the "fresh listener writes new state between our read
    and unlink" race: we hold the listener's flock while comparing+
    unlinking. A live listener holds an exclusive flock for its lifetime
    so our `LOCK_NB` attempt fails and we abort cleanup.

    Returns True iff we unlinked.
    """
    if path is None or expected_state is None:
        return False

    # Derive lock path: `<ppid>.session` → `<ppid>.lock`.
    lock_path: Optional[Path] = None
    if path.name.endswith(".session"):
        lock_path = path.with_name(path.name[: -len(".session")] + ".lock")

    fd = None
    if lock_path is not None and lock_path.exists():
        import fcntl
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Live listener holds the lock — fresh state, abort cleanup.
                os.close(fd)
                return False
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None

    try:
        current = _read_state(path)
        if current is None:
            return False
        if (current.get("session_id") != expected_state.get("session_id") or
                current.get("nonce") != expected_state.get("nonce")):
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False
    finally:
        if fd is not None:
            try:
                os.close(fd)  # also releases the flock
            except OSError:
                pass


def _ppid_of(pid: int) -> Optional[int]:
    proc_status = Path("/proc") / str(pid) / "status"
    if proc_status.exists():
        try:
            for line in proc_status.read_text().splitlines():
                if line.startswith("PPid:"):
                    return int(line.split()[1])
        except (OSError, ValueError):
            return None
    # macOS: shell out to ps
    import subprocess
    try:
        out = subprocess.check_output(["ps", "-p", str(pid), "-o", "ppid="],
                                      stderr=subprocess.DEVNULL).decode().strip()
        return int(out) if out else None
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return None
