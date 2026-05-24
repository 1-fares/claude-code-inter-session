"""Server election + detached spawn. Unix-only for v1."""

from __future__ import annotations

import errno
import fcntl
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from bin import shared

_SERVER_PATH = Path(__file__).parent / "server.py"

# Cap on how long we'll wait for another process's bind-and-spawn election
# to finish before giving up and falling back to a TCP probe.
_ELECTION_WAIT_S = 5.0


def is_server_up(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if is_server_up(host, port):
            return True
        time.sleep(0.05)
    return False


def _acquire_election_lock(host: str, port: int, log) -> int | None:
    """Acquire the cross-process bind-election lock, retrying briefly if it's
    held by a concurrent peer also trying to spawn the server.

    Returns an open fd holding LOCK_EX, or None if the wait deadline expired
    (caller should fall back to a plain `wait_for_server`).

    Why this exists: SO_REUSEADDR on Linux allows two peers to both bind a
    port while neither socket is yet in LISTEN state. Both then spawn
    server.py and the loser crashes with EADDRINUSE inside `listen()`.
    Holding a flock around the bind+spawn+listen sequence serializes the
    election so this race cannot occur."""
    shared.secure_dir(shared.data_dir())
    path = shared.election_lock_path(port, host)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    deadline = time.time() + _ELECTION_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                os.close(fd)
                raise
            # Lock held by another peer. If their spawn has already produced
            # a live server, we don't need the lock at all — bail out.
            if is_server_up(host, port):
                os.close(fd)
                return None
            if time.time() >= deadline:
                os.close(fd)
                log.info("ensure: election lock wait exhausted")
                return None
            time.sleep(0.05)


def _release_election_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def ensure_server_running(
    port: int = shared.DEFAULT_PORT,
    host: str = "127.0.0.1",
    idle_shutdown_minutes: float = 10,
    server_path: Path = _SERVER_PATH,
    python: str = sys.executable,
) -> bool:
    """Ensure a server is listening on (host, port). Race-safe via a
    cross-process bind-election flock plus the kernel's bind() guarantees.

    Returns True if a server is up after this call (either preexisting, or
    freshly spawned by us, or freshly spawned by a peer that won the
    election).
    """
    import logging
    log = logging.getLogger("inter-session.spawn")
    if is_server_up(host, port):
        log.info("ensure: already up")
        return True

    log.info("ensure: not up, contesting election")
    lock_fd = _acquire_election_lock(host, port, log)
    if lock_fd is None:
        # Either the server is up now or another peer is still electing —
        # in both cases the right thing is to wait briefly for it.
        return wait_for_server(host, port, timeout=2.0)

    try:
        # Re-check inside the lock: maybe the previous election winner
        # finished while we were waiting. Saves a wasted bind+spawn.
        if is_server_up(host, port):
            log.info("ensure: server came up while we waited for election")
            return True

        log.info("ensure: election won, attempting bind")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR=1: allow rebind after a previous server crashed (and
        # left the port in TIME_WAIT, especially on macOS). Concurrent-bind
        # race against this flag is now closed by the election flock above.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            s.close()
            log.info("ensure: bind failed errno=%s", e.errno)
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                return wait_for_server(host, port, timeout=2.0)
            raise

        log.info("ensure: bind succeeded; spawning server")
        shared.secure_dir(shared.data_dir())
        shared.ensure_token(shared.token_path())

        os.set_inheritable(s.fileno(), True)
        log_path = shared.server_log_path()
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(
                [
                    python,
                    str(server_path),
                    "--fd", str(s.fileno()),
                    "--host", str(host),
                    "--port", str(port),
                    "--idle-shutdown-minutes", str(idle_shutdown_minutes),
                ],
                pass_fds=(s.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                close_fds=True,
            )
            log.info("ensure: spawned server pid=%s", proc.pid)
        finally:
            os.close(log_fd)
        s.close()
        # Keep the election lock held until the spawned server is actually
        # listening — then any other peer that was waiting for the lock will
        # find `is_server_up` true the moment they get in.
        ready = wait_for_server(host, port, timeout=5.0)
        log.info("ensure: wait_for_server returned %s", ready)
        return ready
    finally:
        _release_election_lock(lock_fd)
