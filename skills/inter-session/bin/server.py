"""WebSocket server for the inter-session bus."""

from __future__ import annotations

# Bootstrap: re-exec under the project's isolated venv if it exists.
# (Server is normally spawned by client.py with sys.executable already
# pointing at the venv; this guards against direct `python3 server.py`
# invocations.)
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
import logging
import signal
import socket
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import websockets
from websockets.server import WebSocketServerProtocol

# Allow running as a script: ensure the skill dir is on sys.path so
# `from bin import shared` finds the package at <skill-dir>/bin/.
_SKILL_DIR = Path(__file__).resolve().parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from bin import shared

log = logging.getLogger("inter-session.server")

# Longest name shared.NAME_RE accepts.
_NAME_MAX_LEN = 40

# On shutdown, time each connection gets to finish its close handshake
# (capped by the send timeout) before it is aborted.
_SHUTDOWN_GRACE_S = 2.0


def _env_seconds(name: str, default: float) -> float:
    """Seconds from environment variable `name`; `default` when it is unset,
    empty or not a number."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _who(state: "ClientState") -> str:
    return state.name or state.session_id[:8]


def _name_candidates(name: str, taken: set, count: int = 3) -> list[str]:
    """Up to `count` names `<name>-<i>` (i >= 2) that are valid and not in
    `taken`. The base is shortened so the suffix fits _NAME_MAX_LEN."""
    out: list[str] = []
    i = 2
    while len(out) < count:
        suffix = f"-{i}"
        cand = name[:_NAME_MAX_LEN - len(suffix)] + suffix
        if cand not in taken:
            out.append(cand)
        i += 1
    return out


@dataclass
class ClientState:
    session_id: str
    role: shared.Role
    name: str
    label: str
    cwd: str
    pid: int
    nonce: str
    ws: WebSocketServerProtocol
    since: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    # Agents only: frames for this peer, written in order by one writer task,
    # so that no handler waits on this peer's socket. Items are
    # (payload, future, queued_at); queued_at is None for a frame that does
    # not expire. None stops the writer.
    outbox: Optional[asyncio.Queue] = None
    outbox_bytes: int = 0
    # No more frames are queued (unregistered or dropped).
    closing: bool = False
    # Dropped because a frame was not taken within the send timeout.
    timed_out: bool = False


class Server:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = shared.DEFAULT_PORT,
        idle_shutdown_minutes: float = 10,
        sock: Optional[socket.socket] = None,
        reap_interval_s: Optional[float] = None,
        reap_miss_threshold: Optional[int] = None,
    ):
        self.host = host
        self.port = port
        self.idle_shutdown_seconds = max(0, idle_shutdown_minutes * 60)
        self._sock = sock
        self._token: Optional[str] = None
        self._registry: dict[str, ClientState] = {}
        self._registry_lock = asyncio.Lock()
        # Rate-limit windows keyed by the *listener* session_id so helpers
        # (control role) can't bypass by opening fresh connections per send.
        self._broadcast_windows: dict[str, deque] = {}
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._last_activity = time.monotonic()
        # Orphan-reap: per-sid consecutive-miss counter. Reset on any
        # observation that the pid is alive and non-zombie.
        self._orphan_misses: dict[str, int] = {}
        # Env-overridable so tests can crank the interval down. <=0 disables.
        env_interval = os.environ.get("INTER_SESSION_REAP_INTERVAL_S")
        env_threshold = os.environ.get("INTER_SESSION_REAP_MISS_THRESHOLD")
        self._reap_interval_s: float = (
            float(env_interval) if env_interval is not None
            else (reap_interval_s if reap_interval_s is not None else shared.REAP_INTERVAL_S)
        )
        self._reap_miss_threshold: int = (
            int(env_threshold) if env_threshold is not None
            else (reap_miss_threshold if reap_miss_threshold is not None else shared.REAP_MISS_THRESHOLD)
        )
        # Bound on every frame write to a peer, and on a direct message's
        # wait in the recipient's queue.
        self._send_timeout_s: float = shared.send_timeout_s()
        # websockets keepalive; <=0 disables.
        ping_interval = _env_seconds(
            "INTER_SESSION_WS_PING_INTERVAL_S", shared.WS_PING_INTERVAL_S)
        ping_timeout = _env_seconds(
            "INTER_SESSION_WS_PING_TIMEOUT_S", shared.WS_PING_TIMEOUT_S)
        self._ws_ping_interval_s: Optional[float] = ping_interval if ping_interval > 0 else None
        self._ws_ping_timeout_s: Optional[float] = ping_timeout if ping_timeout > 0 else None
        # Connection -> state, for every connection past its hello.
        self._links: dict = {}
        # Writers and closes; cancelled on shutdown.
        self._bg_tasks: set = set()

    async def serve(self) -> None:
        shared.secure_dir(shared.data_dir())
        self._token = shared.ensure_token(shared.token_path())

        listen_sock = self._sock
        if listen_sock is None:
            # Direct-bind path: bind ourselves while the socket is NOT yet
            # listening. This keeps the "publish identity before TCP probes can
            # succeed" invariant without trusting a pidfile for a process that
            # may fail to bind because the port is already taken.
            listen_sock = self._bind_socket()

        # Whether the socket came from spawn.py's --fd election or from the
        # direct-bind path above, it is bound but NOT yet listening. Write
        # identity FIRST, then call listen(), so wait_for_server's TCP probe
        # never succeeds before the pidfile exists. Closes the race where a
        # client would refuse to connect ("server identity check failed")
        # because the kernel started accepting connections before identity
        # publish.
        try:
            shared.write_server_identity(os.getpid(), self.host, self.port)
            listen_sock.listen(socket.SOMAXCONN)
            listen_sock.setblocking(False)

            # close_timeout bounds the library's own close handshakes (after a
            # keepalive failure, or when a handler returns) like any send.
            # write_limit=0: ws.send returns only once the kernel holds the
            # whole frame. With the default 64 KiB, it returned while part of
            # the frame was still in asyncio's buffer, which the abort of a
            # dropped peer discards, so a message reported as written could
            # be lost. The kernel's buffer survives the abort.
            kwargs = dict(max_size=shared.WS_FRAME_CAP, max_queue=1,
                          ping_interval=self._ws_ping_interval_s,
                          ping_timeout=self._ws_ping_timeout_s,
                          close_timeout=self._send_timeout_s,
                          write_limit=0)
            server = await websockets.serve(self._handler, sock=listen_sock, **kwargs)
        except Exception:
            listen_sock.close()
            self._unlink_own_identity()
            raise

        self._ready.set()
        log.info("inter-session server listening on %s", self.port)

        idle_task = asyncio.create_task(self._idle_shutdown_loop())
        reap_task = asyncio.create_task(self._orphan_reap_loop())
        try:
            await self._stop.wait()
        finally:
            idle_task.cancel()
            reap_task.cancel()
            server.close()
            await self._abort_unclosed(server)
            await server.wait_closed()
            tasks = list(self._bg_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=1.0)
            # Only remove the pidfile/meta if they still belong to this server
            # — avoids race where a fresh server has already taken over the
            # endpoint and rewritten identity before we exit cleanup.
            self._unlink_own_identity()

    async def _abort_unclosed(self, server) -> None:
        """Shutdown: give each connection a short grace to finish the close
        handshake that server.close() starts, then abort the rest. A peer
        that does not read never answers, and the library would otherwise
        wait up to four times its close timeout for it."""
        conns = list(getattr(server, "websockets", ()))
        if not conns:
            return
        waits = [asyncio.ensure_future(ws.wait_closed()) for ws in conns]
        _done, pending = await asyncio.wait(
            waits, timeout=min(self._send_timeout_s, _SHUTDOWN_GRACE_S))
        for ws in conns:
            if not ws.closed:
                transport = getattr(ws, "transport", None)
                if transport is not None:
                    transport.abort()
        for w in pending:
            w.cancel()

    def _unlink_own_identity(self) -> None:
        for p in (
            shared.pidfile_path(self.port, self.host),
            shared.pidfile_meta_path(self.port, self.host),
        ):
            try:
                if p.name.endswith(".pid"):
                    try:
                        current_pid = int(p.read_text().strip())
                    except (OSError, ValueError):
                        continue
                    if current_pid != os.getpid():
                        continue
                else:
                    try:
                        meta = json.loads(p.read_text())
                        if not isinstance(meta, dict) or meta.get("pid") != os.getpid():
                            continue
                    except (OSError, json.JSONDecodeError):
                        continue
                p.unlink()
            except OSError:
                pass

    def _bind_socket(self) -> socket.socket:
        infos = socket.getaddrinfo(
            self.host,
            self.port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        last_error: Optional[OSError] = None
        for family, socktype, proto, _canonname, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(sockaddr)
                return sock
            except OSError as e:
                last_error = e
                sock.close()
        if last_error is not None:
            raise last_error
        raise OSError(f"could not resolve bind address {self.host!r}:{self.port}")

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def stop(self) -> None:
        self._stop.set()

    async def _idle_shutdown_loop(self) -> None:
        if self.idle_shutdown_seconds <= 0:
            return
        while not self._stop.is_set():
            try:
                await asyncio.sleep(min(5, self.idle_shutdown_seconds))
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            async with self._registry_lock:
                if not self._registry and (now - self._last_activity) >= self.idle_shutdown_seconds:
                    log.info("idle shutdown")
                    self._stop.set()
                    return

    async def _orphan_reap_loop(self) -> None:
        """Safety net for orphaned listeners.

        A listener whose python is alive but orphaned (parent died, harness
        signal didn't reach it) keeps its websocket open and looks
        indistinguishable from a healthy peer on the wire. Periodically we
        re-check each agent's pid via psutil. The only orphan signal we use
        is "pid gone OR zombie/dead", which is unambiguous; PPid-drift is
        intentionally avoided since it false-positives on legitimate
        reparenting on this machine (WSL2 systemd-user, subreaper chains,
        CC harness snapshot-bash recycling). Requires
        REAP_MISS_THRESHOLD consecutive misses before eviction so a
        transient psutil/proc glitch can't mass-reap the registry.

        Evicted listeners are closed with CLOSE_CODE_FORCE_DISCONNECT so
        any still-running python exits its reconnect loop instead of
        rejoining and recreating the ghost.
        """
        if self._reap_interval_s <= 0:
            return
        try:
            import psutil
        except ImportError:
            return  # cannot reap without psutil
        while not self._stop.is_set():
            try:
                try:
                    await asyncio.sleep(self._reap_interval_s)
                except asyncio.CancelledError:
                    return
                if self._stop.is_set():
                    return
                # Snapshot under the registry lock, then do OS calls lock-free.
                async with self._registry_lock:
                    snapshot = [(c.session_id, c) for c in self._registry.values()
                                if c.role == shared.Role.AGENT]
                victims: list[ClientState] = []
                live_sids: set[str] = set()
                for sid, state in snapshot:
                    live_sids.add(sid)
                    pid = state.pid
                    if pid <= 0:
                        self._orphan_misses.pop(sid, None)
                        continue
                    marked = False
                    try:
                        st = psutil.Process(pid).status()
                        if st in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                            marked = True
                    except psutil.NoSuchProcess:
                        marked = True
                    except psutil.AccessDenied:
                        # Transient permission flake — neutral, leave counter alone.
                        continue
                    except Exception:
                        log.exception("orphan-reap: psutil error for pid %s", pid)
                        continue
                    if marked:
                        self._orphan_misses[sid] = self._orphan_misses.get(sid, 0) + 1
                        if self._orphan_misses[sid] >= self._reap_miss_threshold:
                            victims.append(state)
                    else:
                        self._orphan_misses.pop(sid, None)
                # Drop counters for sids that left the registry between ticks.
                for sid in list(self._orphan_misses):
                    if sid not in live_sids:
                        self._orphan_misses.pop(sid, None)
                # Evict victims one by one, re-acquiring the lock with an
                # identity check so we don't close a replaced/reconnected entry.
                for state in victims:
                    async with self._registry_lock:
                        current = self._registry.get(state.session_id)
                        if current is not state:
                            self._orphan_misses.pop(state.session_id, None)
                            continue
                        self._registry.pop(state.session_id, None)
                        self._last_activity = time.monotonic()
                        self._orphan_misses.pop(state.session_id, None)
                    await self._broadcast_event(
                        {"op": "peer_left", "session_id": state.session_id},
                        exclude=state.session_id,
                    )
                    self._spawn(self._close_bounded(
                        state.ws, shared.CLOSE_CODE_FORCE_DISCONNECT, "orphan reap"))
                    log.info(
                        "orphan-reap evicted sid=%s name=%s pid=%s",
                        state.session_id, state.name, state.pid,
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                # Never let a single iteration kill the loop — that would
                # silently disable the safety net for the rest of the
                # server's life.
                log.exception("orphan-reap loop iteration failed")

    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        state: Optional[ClientState] = None
        try:
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                return
            try:
                payload = json.loads(first)
            except json.JSONDecodeError:
                await self._send_error(ws, shared.ErrorCode.UNKNOWN_OP, "malformed JSON")
                return
            if not isinstance(payload, dict):
                await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                       "frame must be a JSON object")
                return
            if payload.get("op") != "hello":
                await self._send_error(ws, shared.ErrorCode.UNKNOWN_OP, "first frame must be hello")
                return
            state = await self._handle_hello(ws, payload)
            if state is None:
                return
            await self._dispatch_loop(state)
        except websockets.ConnectionClosed as e:
            sent = getattr(e, "sent", None)
            if (state is not None and getattr(e, "rcvd", None) is None
                    and sent is not None and sent.code == 1011
                    and sent.reason == "keepalive ping timeout"):
                log.warning("dropping %s %r (sid=%s): no pong within %gs",
                            state.role.value, state.name, state.session_id[:8],
                            self._ws_ping_timeout_s)
        except Exception as e:
            log.exception("handler error: %s", e)
        finally:
            if state is not None:
                await self._unregister(state)

    async def _handle_hello(self, ws, payload) -> Optional[ClientState]:
        if payload.get("token") != self._token:
            await self._send_error(ws, shared.ErrorCode.UNAUTHORIZED, "bad token")
            return None
        # Type-check fields before passing to validators / int(...). Hostile or
        # buggy peers might send `name=null`, `pid="x"`, `label=123`, etc.
        name = payload.get("name", "")
        label = payload.get("label", "")
        if not isinstance(name, str) or not isinstance(label, str):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "name and label must be strings")
            return None
        if name and not shared.validate_name(name):
            await self._send_error(ws, shared.ErrorCode.INVALID_NAME, "invalid name")
            return None
        if not shared.validate_label(label):
            await self._send_error(ws, shared.ErrorCode.INVALID_LABEL, "invalid label")
            return None
        try:
            role = shared.Role(payload.get("role", "agent"))
        except (ValueError, TypeError):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD, "bad role")
            return None
        # Coerce pid; accept missing/invalid as 0.
        try:
            pid_int = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            pid_int = 0
        # Session_id and nonce must be strings if provided.
        sid_raw = payload.get("session_id")
        if sid_raw is not None and not isinstance(sid_raw, str):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "session_id must be a string")
            return None
        nonce_raw = payload.get("nonce", "")
        if not isinstance(nonce_raw, str):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "nonce must be a string")
            return None

        # Control role: act on behalf of an existing agent. Validate `for_session`
        # plus its `nonce` against the registered listener.
        if role == shared.Role.CONTROL:
            for_session = payload.get("for_session", "")
            if not isinstance(for_session, str):
                await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                       "for_session must be a string")
                return None
            reject: Optional[tuple[str, str]] = None
            ctrl_state: Optional[ClientState] = None
            listener_name = listener_label = listener_cwd = ""
            async with self._registry_lock:
                listener = self._registry.get(for_session)
                if listener is None or listener.role != shared.Role.AGENT:
                    reject = (shared.ErrorCode.UNKNOWN_PEER,
                              f"no listener for {for_session!r}")
                elif not nonce_raw or nonce_raw != listener.nonce:
                    reject = (shared.ErrorCode.UNAUTHORIZED,
                              "stale listener state; reconnect")
                else:
                    listener_name = listener.name
                    listener_label = listener.label
                    listener_cwd = listener.cwd
                    ctrl_state = ClientState(
                        session_id=str(uuid.uuid4()),
                        role=role,
                        name=listener_name,
                        label=listener_label,
                        cwd=listener_cwd,
                        pid=pid_int,
                        nonce=nonce_raw,
                        ws=ws,
                    )
                    ctrl_state._for_session = for_session  # type: ignore[attr-defined]
                    self._links[ws] = ctrl_state
            if reject is not None:
                code, message = reject
                await self._send_error(ws, code, message)
                return None
            await self._send_frame(ws, {
                "op": "welcome",
                "session_id": ctrl_state.session_id,
                "assigned_name": listener_name,
                "for_session": for_session,
            })
            return ctrl_state

        # Agent role
        sid = sid_raw or str(uuid.uuid4())
        cwd_raw = payload.get("cwd", "")
        if not isinstance(cwd_raw, str):
            cwd_raw = ""
        # Sanitize cwd before storing — it's reflected through `list_ok` and
        # rendered by helpers' table output. Prevents terminal-escape
        # injection by a hostile (or path-with-control-chars) peer.
        cwd_clean = shared.sanitize_for_stdout(cwd_raw)[:256]
        # Defer the close of any replaced ws AND error-send to AFTER lock
        # release so neither stalls other handlers that need the lock.
        old_ws_to_close = None
        reject: Optional[tuple[str, str, dict]] = None  # (code, message, extras)
        state: Optional[ClientState] = None
        async with self._registry_lock:
            existing_same_sid = self._registry.get(sid)
            if existing_same_sid is not None:
                # Reconnect-replace must prove continuity via the nonce. The
                # session_id leaks via `list_ok` and msg metadata, so anyone
                # with the token could otherwise impersonate / kick a peer
                # by reusing its session_id.
                if not nonce_raw or nonce_raw != existing_same_sid.nonce:
                    reject = (
                        shared.ErrorCode.UNAUTHORIZED,
                        "session_id is in use by another connection",
                        {},
                    )
            if reject is None and name:
                # The entry this hello replaces (same sid) does not hold the
                # name against it. Nothing is popped before this check, so a
                # rejected hello leaves the registry as it was.
                taken = {c.name for c in self._registry.values()
                         if c.role == shared.Role.AGENT and c.session_id != sid}
                if name in taken:
                    reject = (
                        shared.ErrorCode.NAME_TAKEN,
                        f"name {name!r} is taken",
                        {"candidates": _name_candidates(name, taken)},
                    )
            if reject is None:
                if existing_same_sid is not None:
                    old_ws_to_close = existing_same_sid.ws
                    self._registry.pop(sid, None)
                state = ClientState(
                    session_id=sid,
                    role=role,
                    name=name,
                    label=label,
                    cwd=cwd_clean,
                    pid=pid_int,
                    nonce=nonce_raw,
                    ws=ws,
                )
                self._registry[sid] = state
                self._last_activity = time.monotonic()
                # Queued under the lock, so no frame for this peer can get
                # ahead of its welcome.
                self._start_writer(state, json.dumps({
                    "op": "welcome",
                    "session_id": sid,
                    "assigned_name": name,
                }))
        # Lock released — now do any blocking network work.
        if reject is not None:
            code, message, extras = reject
            await self._send_error(ws, code, message, **extras)
            return None
        if old_ws_to_close is not None:
            self._spawn(self._close_bounded(old_ws_to_close, 4000, "replaced"))
        await self._broadcast_event({"op": "peer_joined", "session_id": sid, "name": name},
                                    exclude=sid)
        return state

    async def _dispatch_loop(self, state: ClientState) -> None:
        async for raw in state.ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_OP, "malformed JSON")
                continue
            if not isinstance(payload, dict):
                await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                       "frame must be a JSON object")
                continue
            op = payload.get("op")
            if op == "ping":
                await self._send_frame(state.ws, {"op": "pong"})
            elif op == "list":
                await self._handle_list(state)
            elif op == "send":
                await self._handle_send(state, payload)
            elif op == "broadcast":
                await self._handle_broadcast(state, payload)
            elif op == "rename":
                await self._handle_rename(state, payload)
            elif op == "force_disconnect":
                await self._handle_force_disconnect(state)
            elif op == "bye":
                return
            elif op == "hello":
                await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_OP, "duplicate hello")
            else:
                await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_OP, f"unknown op {op!r}")

    async def _handle_list(self, state: ClientState) -> None:
        # Same liveness invariant as send/broadcast/rename: a control whose
        # listener has disappeared shouldn't be able to keep enumerating peers.
        reject: Optional[tuple[str, str]] = None
        sessions: list = []
        async with self._registry_lock:
            if not self._listener_alive_locked(state):
                reject = (shared.ErrorCode.UNAUTHORIZED, "listener no longer connected")
            else:
                sessions = [
                    {
                        "session_id": c.session_id,
                        "name": c.name,
                        "label": c.label,
                        "cwd": c.cwd,
                        "since": c.since.isoformat(),
                    }
                    for c in self._registry.values()
                    if c.role == shared.Role.AGENT
                ]
        if reject is not None:
            code, message = reject
            await self._send_error(state.ws, code, message)
            return
        await self._send_frame(state.ws, {"op": "list_ok", "sessions": sessions})

    def _from_id_for(self, state: ClientState) -> str:
        if state.role == shared.Role.CONTROL:
            return getattr(state, "_for_session", state.session_id)
        return state.session_id

    def _listener_alive_locked(self, state: ClientState) -> bool:
        """Caller must hold _registry_lock. For agent role: alive iff the
        connection is open (always True from a handler's perspective; the
        handler exits on disconnect). For control role: alive iff the
        impersonated listener is still in the registry."""
        if state.role != shared.Role.CONTROL:
            return True
        listener_sid = getattr(state, "_for_session", None)
        if not listener_sid:
            return False
        return listener_sid in self._registry

    async def _resolve_send_target(
        self, state: ClientState, target: str,
    ) -> tuple[Optional[ClientState], Optional[tuple[str, str, dict]]]:
        """Combined liveness + target resolution under a single lock.
        Returns (target_state, None) on success, (None, (code, msg, extras))
        on rejection."""
        from_id = self._from_id_for(state)
        async with self._registry_lock:
            if not self._listener_alive_locked(state):
                return None, (shared.ErrorCode.UNAUTHORIZED,
                              "listener no longer connected", {})
            agents = [c for c in self._registry.values() if c.role == shared.Role.AGENT]
            # 1) exact session_id
            for c in agents:
                if c.session_id == target:
                    if c.session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return c, None
            # 2) exact name
            named = [c for c in agents if c.name]
            for c in named:
                if c.name == target:
                    if c.session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return c, None
            # 3) name prefix
            if target:
                matches = [c for c in named if c.name.startswith(target)]
                if len(matches) == 1:
                    if matches[0].session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return matches[0], None
                if len(matches) > 1:
                    return None, (shared.ErrorCode.AMBIGUOUS,
                                  f"ambiguous prefix {target!r}",
                                  {"matches": [c.name for c in matches]})
            # 4) session_id prefix (for the short-id shown in `list` output).
            #    Only kicks in if no name matched. Require ≥4 chars to avoid
            #    accidental matches.
            if target and len(target) >= 4:
                sid_matches = [c for c in agents if c.session_id.startswith(target)]
                if len(sid_matches) == 1:
                    if sid_matches[0].session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return sid_matches[0], None
                if len(sid_matches) > 1:
                    return None, (shared.ErrorCode.AMBIGUOUS,
                                  f"ambiguous session_id prefix {target!r}",
                                  {"matches": [c.session_id[:8] for c in sid_matches]})
            return None, (shared.ErrorCode.UNKNOWN_PEER,
                          f"no agent matches {target!r}", {})

    async def _resolve_broadcast_targets(
        self, state: ClientState, from_id: str,
    ) -> tuple[list, Optional[tuple[str, str]]]:
        """Combined liveness + targets snapshot. Returns (targets, None) on
        success, ([], (code, msg)) on rejection."""
        async with self._registry_lock:
            if not self._listener_alive_locked(state):
                return [], (shared.ErrorCode.UNAUTHORIZED,
                            "listener no longer connected")
            targets = [c for c in self._registry.values()
                       if c.role == shared.Role.AGENT and c.session_id != from_id]
            return targets, None

    async def _handle_send(self, state: ClientState, payload) -> None:
        text = payload.get("text", "")
        if not isinstance(text, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "text must be a string")
            return
        if len(text) > shared.TEXT_CAP:
            await self._send_error(state.ws, shared.ErrorCode.TEXT_TOO_LONG,
                                   "text exceeds direct send cap")
            return
        target = payload.get("to", "")
        if not isinstance(target, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "to must be a string")
            return
        # Single locked phase: verify listener liveness AND resolve target,
        # so a control whose listener disappears can't slip a message
        # through between the liveness check and the send.
        target_state, reject = await self._resolve_send_target(state, target)
        if reject is not None:
            code, message, extras = reject
            await self._send_error(state.ws, code, message, **extras)
            return
        msg = {
            "op": "msg",
            "msg_id": uuid.uuid4().hex[:8],
            "from": self._from_id_for(state),
            "from_name": state.name,
            "from_label": state.label,
            "to": target_state.name,
            "to_session_id": target_state.session_id,
            "text": text,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }
        self._log_message(msg, kind="direct")
        # The recipient's writer delivers it; only this sender waits for the
        # outcome. The writer takes the message out of the queue within the
        # send timeout or reports delivery_timeout, and bounds its write by
        # the send timeout, so the outcome is known within twice the send
        # timeout. Its error frame must come ahead of the reply to the
        # sender's next frame: send.py reads the outcome from that order.
        fut = self._enqueue(target_state, json.dumps(msg), expires=True)
        failure = self._not_queued(target_state) if fut is None else await fut
        self._last_activity = time.monotonic()
        if failure is not None:
            code, message = failure
            await self._send_error(state.ws, code, message)

    async def _handle_broadcast(self, state: ClientState, payload) -> None:
        text = payload.get("text", "")
        if not isinstance(text, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "text must be a string")
            return
        if len(text) > shared.BROADCAST_TEXT_CAP:
            await self._send_error(state.ws, shared.ErrorCode.TEXT_TOO_LONG,
                                   "text exceeds broadcast cap")
            return
        from_id = self._from_id_for(state)
        # Liveness + targets snapshot first — a stale control connection
        # whose listener has gone away should not burn a rate-limit slot.
        targets, reject = await self._resolve_broadcast_targets(state, from_id)
        if reject is not None:
            code, message = reject
            await self._send_error(state.ws, code, message)
            return
        # Rate limit keyed by listener session_id, not per-connection: helpers
        # (role=control) open a fresh connection per send and would otherwise
        # bypass the cap. Charge AFTER liveness so dead-listener attempts
        # don't consume quota.
        now = time.monotonic()
        # Drop windows whose newest entry is past the 60 s horizon: they limit
        # nothing, and kept they grow the dict by one entry per session that
        # ever broadcast.
        for sid in [k for k, w in self._broadcast_windows.items()
                    if not w or (now - w[-1]) > 60]:
            del self._broadcast_windows[sid]
        window = self._broadcast_windows.setdefault(from_id, deque())
        while window and (now - window[0]) > 60:
            window.popleft()
        if len(window) >= shared.BROADCAST_RATE_LIMIT_PER_MIN:
            await self._send_error(state.ws, shared.ErrorCode.RATE_LIMITED,
                                   "broadcast rate limit exceeded")
            return
        window.append(now)
        msg = {
            "op": "msg",
            "msg_id": uuid.uuid4().hex[:8],
            "from": from_id,
            "from_name": state.name,
            "from_label": state.label,
            "text": text,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }
        self._log_message(msg, kind="broadcast")
        payload = json.dumps(msg)
        for target in targets:
            self._enqueue(target, payload)
        self._last_activity = time.monotonic()

    def _log_message(self, msg: dict, kind: str) -> None:
        """Single-writer JSONL log on the server side. Eliminates the
        previous double-write where every receiver appended the same msg.
        Rotates by size."""
        try:
            shared.secure_dir(shared.data_dir())
            path = shared.messages_log_path()
            shared.rotate_log_if_needed(
                path, shared.MESSAGES_LOG_MAX_BYTES, shared.MESSAGES_LOG_BACKUPS,
            )
            rec = {
                "ts": msg["ts"],
                "msg_id": msg["msg_id"],
                "kind": kind,
                "from": msg["from"],
                "from_name": msg["from_name"],
                "from_label": msg.get("from_label", ""),
                "to": msg.get("to", ""),
                "to_session_id": msg.get("to_session_id", ""),
                "text": msg["text"],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as e:
            # Delivery goes on; record that the durable copy is missing.
            log.warning("messages.log write failed for msg_id=%s: %s",
                        msg.get("msg_id", ""), e)

    async def _handle_rename(self, state: ClientState, payload) -> None:
        new_name = payload.get("name", "")
        if not isinstance(new_name, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "name must be a string")
            return
        if new_name and not shared.validate_name(new_name):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_NAME, "invalid name")
            return
        # Resolve the listener entry to mutate. For agent role it's `state` itself;
        # for control role it's the registered listener whose session_id is `_for_session`.
        target_sid = self._from_id_for(state)
        reject: Optional[tuple[str, str]] = None  # (code, message)
        async with self._registry_lock:
            target = self._registry.get(target_sid)
            if target is None or target.role != shared.Role.AGENT:
                reject = (shared.ErrorCode.UNKNOWN_PEER, "no listener to rename")
            elif new_name:
                for c in self._registry.values():
                    if (c.session_id != target_sid and
                            c.role == shared.Role.AGENT and c.name == new_name):
                        reject = (shared.ErrorCode.NAME_TAKEN, f"name {new_name!r} is taken")
                        break
            if reject is None:
                target.name = new_name
                if state is not target:
                    state.name = new_name
        if reject is not None:
            code, message = reject
            await self._send_error(state.ws, code, message)
            return
        await self._send_frame(state.ws, {"op": "renamed", "name": new_name})
        await self._broadcast_event(
            {"op": "renamed", "session_id": target_sid, "name": new_name},
            exclude=target_sid,
        )

    async def _handle_force_disconnect(self, state: ClientState) -> None:
        """Control-only: close the listener this control connection acts for.

        Frees the name on the bus immediately — the listener's `session_id` is
        popped from the registry and `peer_left` is broadcast before the ack
        returns to the caller. The listener's websocket is then closed with
        `CLOSE_CODE_FORCE_DISCONNECT` so that any still-running client process
        exits its reconnect loop instead of rejoining under the same name.

        This is the primary mechanism behind `/is d`: it does not depend on
        the harness delivering any OS signal to the client, so it works even
        when the harness `TaskStop` is a no-op.
        """
        if state.role != shared.Role.CONTROL:
            await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_OP,
                                   "force_disconnect is control-only")
            return
        target_sid = getattr(state, "_for_session", None)
        if not target_sid:
            await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_PEER,
                                   "no target listener for this control")
            return
        target: Optional[ClientState] = None
        async with self._registry_lock:
            existing = self._registry.get(target_sid)
            if existing is not None and existing.role == shared.Role.AGENT:
                self._registry.pop(target_sid, None)
                self._last_activity = time.monotonic()
                target = existing
        if target is None:
            # Already gone — idempotent ack so the caller still gets a clean
            # reply (e.g. when /is d races a parallel client-side close).
            await self._send_frame(state.ws, {
                "op": "force_disconnect_ok",
                "session_id": target_sid,
                "was_present": False,
            })
            return
        # Order: announce peer_left before closing the target ws, so other
        # peers see the departure cleanly. The target's own _handler finally
        # will also call _unregister, but its identity check is a no-op since
        # we already popped. The close runs in its own task: a target that
        # does not read must not hold up this control connection.
        await self._broadcast_event(
            {"op": "peer_left", "session_id": target_sid},
            exclude=target_sid,
        )
        self._spawn(self._close_bounded(
            target.ws, shared.CLOSE_CODE_FORCE_DISCONNECT, "session ended by owner"))
        await self._send_frame(state.ws, {
            "op": "force_disconnect_ok",
            "session_id": target_sid,
            "was_present": True,
        })

    async def _broadcast_event(self, msg: dict, exclude: Optional[str] = None) -> None:
        async with self._registry_lock:
            targets = [c for c in self._registry.values()
                       if c.role == shared.Role.AGENT and c.session_id != exclude]
        payload = json.dumps(msg)
        for c in targets:
            self._enqueue(c, payload)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _start_writer(self, state: ClientState, first: str) -> None:
        """Give an agent its outbox with `first` as the first frame, and the
        task that writes it."""
        state.outbox = asyncio.Queue()
        self._links[state.ws] = state
        self._enqueue(state, first)
        self._spawn(self._write_loop(state))

    def _stop_writer(self, state: ClientState) -> None:
        """Queue nothing more for `state`; its writer ends after the frames
        already queued."""
        if self._links.get(state.ws) is state:
            self._links.pop(state.ws, None)
        if state.outbox is not None and not state.closing:
            state.closing = True
            state.outbox.put_nowait(None)

    def _enqueue(self, peer: ClientState, payload: str,
                 expires: bool = False) -> Optional[asyncio.Future]:
        """Queue one frame for `peer` without waiting. The future resolves to
        None once the frame is written, or to an (error code, message) pair
        when it is not. With `expires`, the frame is not written once it has
        waited the send timeout in the queue. Returns None if nothing was
        queued: the peer is closing, or it already holds OUTBOX_MAX_BYTES it
        has not taken."""
        if peer.outbox is None or peer.closing:
            return None
        if peer.outbox_bytes + len(payload) > shared.OUTBOX_MAX_BYTES:
            return None
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        peer.outbox_bytes += len(payload)
        peer.outbox.put_nowait((payload, fut, loop.time() if expires else None))
        return fut

    async def _write_loop(self, peer: ClientState) -> None:
        """Write `peer`'s queued frames in order, each bounded by the send
        timeout. Frames behind one that timed out are not written. An
        expiring frame that waited the send timeout in the queue is not
        written either; the peer stays connected."""
        loop = asyncio.get_running_loop()
        while True:
            item = await peer.outbox.get()
            if item is None:
                return
            payload, fut, queued_at = item
            peer.outbox_bytes -= len(payload)
            waited = 0.0 if queued_at is None else loop.time() - queued_at
            if not peer.timed_out and waited >= self._send_timeout_s:
                if not fut.done():
                    fut.set_result((
                        shared.ErrorCode.DELIVERY_TIMEOUT,
                        f"message to {_who(peer)} waited {waited:.1f}s behind "
                        "other frames and was not delivered"))
                continue
            try:
                written = not peer.timed_out and await self._send_bounded(
                    peer.ws, payload, peer)
            except Exception:
                log.exception("write to %r failed", _who(peer))
                written = False
            if written:
                failure = None
            elif peer.timed_out:
                failure = self._timeout_failure(peer)
            else:
                failure = (shared.ErrorCode.UNKNOWN_PEER,
                           f"{_who(peer)} disconnected before delivery")
            if not fut.done():
                fut.set_result(failure)

    def _timeout_failure(self, peer: ClientState) -> tuple[str, str]:
        return (shared.ErrorCode.DELIVERY_TIMEOUT,
                f"{_who(peer)} took no data within {self._send_timeout_s:g}s "
                "and was disconnected; message not delivered")

    def _not_queued(self, peer: ClientState) -> tuple[str, str]:
        if peer.timed_out:
            return self._timeout_failure(peer)
        if peer.closing:
            return (shared.ErrorCode.UNKNOWN_PEER,
                    f"{_who(peer)} disconnected before delivery")
        return (shared.ErrorCode.DELIVERY_TIMEOUT,
                f"{_who(peer)} is not taking data; message not delivered")

    async def _send_bounded(self, ws, payload: str,
                            peer: Optional[ClientState] = None) -> bool:
        """Write one frame to `ws` within the send timeout. On timeout the
        connection is dropped. True if the frame was written."""
        try:
            await asyncio.wait_for(ws.send(payload), timeout=self._send_timeout_s)
        except asyncio.TimeoutError:
            await self._drop(ws, peer)
            return False
        except websockets.ConnectionClosed:
            return False
        # A drop while this send waited wakes it as if the frame was written.
        return not (peer is not None and peer.timed_out)

    async def _drop(self, ws, peer: Optional[ClientState]) -> None:
        """Abort a connection that took no frame within the send timeout and
        unregister its peer. Logs one line, without the frame."""
        transport = getattr(ws, "transport", None)
        if peer is not None:
            if peer.timed_out:
                return
            peer.timed_out = True
            log.warning("dropping %s %r (sid=%s): no data taken within %gs",
                        peer.role.value, peer.name, peer.session_id[:8],
                        self._send_timeout_s)
        elif transport is not None and not transport.is_closing():
            log.warning("dropping unregistered connection: no data taken within %gs",
                        self._send_timeout_s)
        if transport is not None:
            transport.abort()
        if peer is not None:
            await self._unregister(peer)

    async def _close_bounded(self, ws, code: int, reason: str) -> None:
        """Close `ws`, aborting it if the close handshake has not finished
        within the send timeout: a peer that does not read never answers."""
        task = asyncio.ensure_future(ws.close(code=code, reason=reason))
        try:
            done, _ = await asyncio.wait({task}, timeout=self._send_timeout_s)
            if not done:
                await self._drop(ws, self._links.get(ws))
                await asyncio.wait({task}, timeout=self._send_timeout_s)
        finally:
            if not task.done():
                task.cancel()
            elif not task.cancelled():
                task.exception()  # retrieve it; a failed close needs no report

    async def _send_frame(self, ws, frame: dict) -> None:
        """Send one frame to connection `ws`. A registered agent gets it
        through its outbox and this does not wait; any other connection is
        written to directly, within the send timeout."""
        payload = json.dumps(frame)
        peer = self._links.get(ws)
        if peer is not None and peer.outbox is not None:
            self._enqueue(peer, payload)
            return
        await self._send_bounded(ws, payload, peer)

    async def _unregister(self, state: ClientState) -> None:
        self._stop_writer(state)
        async with self._registry_lock:
            current = self._registry.get(state.session_id)
            if current is state:
                self._registry.pop(state.session_id, None)
                self._last_activity = time.monotonic()
                should_announce = state.role == shared.Role.AGENT
            else:
                should_announce = False
        if should_announce:
            await self._broadcast_event(
                {"op": "peer_left", "session_id": state.session_id},
                exclude=state.session_id,
            )

    async def _send_error(self, ws, code: str, message: str, **extra) -> None:
        await self._send_frame(ws, {"op": "error", "code": code, "message": message, **extra})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=shared.DEFAULT_PORT)
    parser.add_argument("--fd", type=int, default=None,
                        help="Adopt an inherited bound socket fd (set by client.py)")
    parser.add_argument("--idle-shutdown-minutes", type=float, default=10)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    # Quiet `websockets.server` INFO chatter ("connection rejected (400)"
    # etc.) — these are mostly from `is_server_up()` doing a TCP probe with
    # no Upgrade header, which is expected behavior, not a problem.
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    sock = None
    if args.fd is not None:
        # Adopt the bound (NOT yet listening) fd. Server.serve() will write
        # identity FIRST, then listen() — closing the race where
        # wait_for_server's TCP probe sees "server up" before pidfile exists.
        sock = socket.socket(fileno=args.fd)
        sock.setblocking(False)

    srv = Server(
        host=args.host,
        port=args.port,
        idle_shutdown_minutes=args.idle_shutdown_minutes,
        sock=sock,
    )
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, srv.stop)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(srv.serve())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
