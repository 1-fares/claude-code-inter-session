"""A peer that stops reading must not stall the server for other peers.

The stuck peer completes the hello, then its client stops reading at the
transport level. Its socket buffers are shrunk on both ends, so a filler
message larger than the buffers keeps the server's write to it pending
whatever the tcp_wmem/tcp_rmem settings. INTER_SESSION_SEND_TIMEOUT_S
bounds that write.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import socket
import time

import websockets

from bin import shared
from bin.server import Server
from tests.test_server import _drain, _hello, _recv, _recv_until, _send_op

SEND_TIMEOUT_S = 2.0
# Bound for anything that must not wait on the stuck peer.
PROMPT_S = 1.0
# Random, so permessage-deflate cannot shrink it below the socket buffers.
FILLER = base64.b64encode(os.urandom(3 * 1024 * 1024)).decode()


@contextlib.asynccontextmanager
async def _serving(tmp_data_dir, port, monkeypatch, **env):
    settings = {"INTER_SESSION_SEND_TIMEOUT_S": str(SEND_TIMEOUT_S)}
    settings.update(env)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    shared.secure_dir(tmp_data_dir)
    token = shared.ensure_token(shared.token_path())
    srv = Server(host="127.0.0.1", port=port, idle_shutdown_minutes=10)
    task = asyncio.create_task(srv.serve())
    await srv.wait_ready()
    clients: list = []
    try:
        yield srv, task, token, clients
    finally:
        # Abort the clients first: an unfixed server only unblocks once the
        # stuck peer's connection is gone.
        for ws in clients:
            transport = getattr(ws, "transport", None)
            if transport is not None:
                transport.abort()
        srv.stop()
        await asyncio.wait({task}, timeout=5.0)
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def _agent(port, token, name, clients, **extra):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/", max_size=shared.WS_FRAME_CAP)
    clients.append(ws)
    sid, welcome = await _hello(ws, token, name=name, **extra)
    assert welcome["op"] == "welcome"
    return ws, sid


async def _stuck_agent(srv, port, token, name, clients, **extra):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect(("127.0.0.1", port))
    sock.setblocking(False)
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/", sock=sock,
        max_size=shared.WS_FRAME_CAP, ping_interval=None,
    )
    clients.append(ws)
    sid, welcome = await _hello(ws, token, name=name, **extra)
    assert welcome["op"] == "welcome"
    ws.transport.pause_reading()
    server_sock = srv._registry[sid].ws.transport.get_extra_info("socket")
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    return ws, sid


async def _fill(srv, sender_ws, stuck_name, stuck_sid):
    """Queue FILLER for the stuck peer and wait until the server holds most
    of it unsent, i.e. its write to that peer is pending."""
    transport = srv._registry[stuck_sid].ws.transport
    await _send_op(sender_ws, "send", to=stuck_name, text=FILLER)
    deadline = time.monotonic() + PROMPT_S
    while transport.get_write_buffer_size() < 1024 * 1024:
        assert time.monotonic() < deadline, "stuck state not reached"
        await asyncio.sleep(0.02)


async def _names(ws, timeout=PROMPT_S):
    await _send_op(ws, "list")
    listing = await _recv_until(ws, "list_ok", timeout=timeout)
    assert listing["op"] == "list_ok", listing
    return {s["name"] for s in listing["sessions"]}


async def _errors_before_pong(ws, timeout):
    """Send a ping behind the frames already sent on `ws`, as send.py does,
    and return the error frames that arrive ahead of its pong."""
    await _send_op(ws, "ping")
    errors = []
    deadline = time.monotonic() + timeout
    while True:
        frame = await _recv(ws, timeout=max(deadline - time.monotonic(), 0.01))
        if frame.get("op") == "pong":
            return errors
        if frame.get("op") == "error":
            errors.append(frame)


async def _read_to_end(ws, timeout):
    """Texts of the `msg` frames `ws` receives until its connection closes."""
    texts = []
    deadline = time.monotonic() + timeout
    with contextlib.suppress(websockets.ConnectionClosed):
        while True:
            raw = await asyncio.wait_for(ws.recv(), max(deadline - time.monotonic(), 0.01))
            frame = json.loads(raw)
            if frame.get("op") == "msg":
                texts.append(frame["text"])
    return texts


async def test_stuck_peer_does_not_block_join_list_send_broadcast(
    tmp_data_dir, free_port, monkeypatch,
):
    async with _serving(tmp_data_dir, free_port, monkeypatch) as (srv, _task, token, clients):
        healthy, _ = await _agent(free_port, token, "healthy", clients)
        _, stuck_sid = await _stuck_agent(srv, free_port, token, "stuck", clients)
        filler, _ = await _agent(free_port, token, "filler", clients)
        await _fill(srv, filler, "stuck", stuck_sid)
        t0 = time.monotonic()

        newcomer, _ = await asyncio.wait_for(
            _agent(free_port, token, "newcomer", clients), PROMPT_S)
        # Still registered: the stuck state is in effect for what follows.
        assert "stuck" in await _names(newcomer)

        await _send_op(newcomer, "send", to="healthy", text="direct-1")
        msg = await _recv_until(healthy, "msg", timeout=PROMPT_S)
        assert msg["text"] == "direct-1"
        await _send_op(newcomer, "broadcast", text="all-1")
        msg = await _recv_until(healthy, "msg", timeout=PROMPT_S)
        assert msg["text"] == "all-1"
        assert [f for f in await _drain(newcomer, timeout=0.2) if f.get("op") == "error"] == []
        assert time.monotonic() - t0 < SEND_TIMEOUT_S


async def test_stuck_peer_dropped_once_and_sender_gets_error(
    tmp_data_dir, free_port, monkeypatch, caplog,
):
    caplog.set_level(logging.INFO, logger="inter-session.server")
    async with _serving(tmp_data_dir, free_port, monkeypatch) as (srv, _task, token, clients):
        healthy, _ = await _agent(free_port, token, "healthy", clients)
        stuck, stuck_sid = await _stuck_agent(srv, free_port, token, "stuck", clients)
        filler, _ = await _agent(free_port, token, "filler", clients)
        await _fill(srv, filler, "stuck", stuck_sid)
        # A control connection, as send.py opens, acting for agent "owner".
        _, owner_sid = await _agent(free_port, token, "owner", clients, nonce="n-owner")
        sender, _ = await _agent(free_port, token, "", clients, role=shared.Role.CONTROL,
                                 for_session=owner_sid, nonce="n-owner")
        await _send_op(sender, "send", to="stuck", text="SECRET-BODY")
        # send.py sends a ping right behind the send and takes the order of
        # the replies as the outcome: the error must come first.
        await _send_op(sender, "ping")

        err = await _recv_until(sender, ("error", "pong"), timeout=SEND_TIMEOUT_S + 2)
        assert err["op"] == "error" and err["code"] == shared.ErrorCode.DELIVERY_TIMEOUT
        pong = await _recv_until(sender, "pong", timeout=PROMPT_S)
        assert pong["op"] == "pong"
        err = await _recv_until(filler, "error", timeout=PROMPT_S)
        assert err["op"] == "error" and err["code"] == shared.ErrorCode.DELIVERY_TIMEOUT

        frames = await _drain(healthy, timeout=0.5)
        left = [f for f in frames
                if f.get("op") == "peer_left" and f.get("session_id") == stuck_sid]
        assert len(left) == 1, frames
        assert "stuck" not in await _names(healthy)
        assert stuck_sid not in srv._registry

        # The server closed the connection: the client sees it once it reads.
        stuck.transport.resume_reading()
        await asyncio.wait_for(stuck.wait_closed(), PROMPT_S)

        lines = [r.getMessage() for r in caplog.records
                 if r.levelno >= logging.WARNING and "stuck" in r.getMessage()]
        assert len(lines) == 1, lines
        assert "SECRET-BODY" not in caplog.text
        assert FILLER[:64] not in caplog.text


async def test_force_disconnect_of_stuck_peer_answers_promptly(
    tmp_data_dir, free_port, monkeypatch,
):
    async with _serving(tmp_data_dir, free_port, monkeypatch) as (srv, _task, token, clients):
        healthy, _ = await _agent(free_port, token, "healthy", clients)
        _, stuck_sid = await _stuck_agent(srv, free_port, token, "stuck", clients,
                                          nonce="n-stuck")
        filler, _ = await _agent(free_port, token, "filler", clients)
        await _fill(srv, filler, "stuck", stuck_sid)
        ctrl, _ = await _agent(free_port, token, "", clients, role=shared.Role.CONTROL,
                               for_session=stuck_sid, nonce="n-stuck")
        await _send_op(ctrl, "force_disconnect")
        ack = await _recv_until(ctrl, "force_disconnect_ok", timeout=PROMPT_S)
        assert ack["op"] == "force_disconnect_ok" and ack["was_present"] is True
        ev = await _recv_until(healthy, "peer_left", timeout=PROMPT_S)
        assert ev["op"] == "peer_left" and ev["session_id"] == stuck_sid


async def test_shutdown_completes_with_stuck_peer(tmp_data_dir, free_port, monkeypatch):
    # A long send bound keeps the peer stuck while the server stops.
    async with _serving(tmp_data_dir, free_port, monkeypatch,
                        INTER_SESSION_SEND_TIMEOUT_S="30") as (srv, task, token, clients):
        await _agent(free_port, token, "healthy", clients)
        _, stuck_sid = await _stuck_agent(srv, free_port, token, "stuck", clients)
        filler, _ = await _agent(free_port, token, "filler", clients)
        await _fill(srv, filler, "stuck", stuck_sid)
        t0 = time.monotonic()
        srv.stop()
        await asyncio.wait({task}, timeout=4.0)
        assert task.done(), "server stop did not finish within 4 s"
        assert task.exception() is None
        assert time.monotonic() - t0 < 4.0


async def test_order_kept_per_recipient_while_peer_stuck(tmp_data_dir, free_port, monkeypatch):
    async with _serving(tmp_data_dir, free_port, monkeypatch) as (srv, _task, token, clients):
        healthy, _ = await _agent(free_port, token, "healthy", clients)
        _, stuck_sid = await _stuck_agent(srv, free_port, token, "stuck", clients)
        filler, _ = await _agent(free_port, token, "filler", clients)
        await _fill(srv, filler, "stuck", stuck_sid)
        sender, _ = await _agent(free_port, token, "sender", clients)
        expected = []
        for i in range(20):
            await _send_op(sender, "send", to="healthy", text=f"m{i}")
            expected.append(f"m{i}")
            if i % 4 == 0:
                # Also goes to the stuck peer.
                await _send_op(sender, "broadcast", text=f"b{i}")
                expected.append(f"b{i}")
        got = []
        while len(got) < len(expected):
            msg = await _recv_until(healthy, "msg", timeout=PROMPT_S)
            assert msg["op"] == "msg", msg
            got.append(msg["text"])
        assert got == expected


async def test_keepalive_drops_silent_peer_without_traffic(tmp_data_dir, free_port, monkeypatch):
    # After a keepalive failure the library waits close_timeout (the send
    # bound) for the peer's TCP close before the connection counts as closed.
    async with _serving(tmp_data_dir, free_port, monkeypatch,
                        INTER_SESSION_SEND_TIMEOUT_S="0.5",
                        INTER_SESSION_WS_PING_INTERVAL_S="0.3",
                        INTER_SESSION_WS_PING_TIMEOUT_S="0.3") as (srv, _task, token, clients):
        healthy, _ = await _agent(free_port, token, "healthy", clients)
        _, silent_sid = await _stuck_agent(srv, free_port, token, "silent", clients)
        ev = await _recv_until(healthy, "peer_left", timeout=3.0)
        assert ev["op"] == "peer_left" and ev["session_id"] == silent_sid


async def test_keepalive_keeps_idle_live_client(tmp_data_dir, free_port, monkeypatch):
    """Guard for the keepalive setting: a client that sends nothing but whose
    websockets library runs answers the protocol pings and stays.

    Client and server share one event loop, so a stall of the host between
    a ping and the handling of its pong counts against the ping timeout. The
    2 s timeout leaves a one-second stall a second of room. A client that
    did not answer would be gone after 3.5 s (first ping at 0.5 s, its
    timeout, then at most the 1 s send bound for the close), within the
    4 s watched."""
    async with _serving(tmp_data_dir, free_port, monkeypatch,
                        INTER_SESSION_SEND_TIMEOUT_S="1.0",
                        INTER_SESSION_WS_PING_INTERVAL_S="0.5",
                        INTER_SESSION_WS_PING_TIMEOUT_S="2.0") as (srv, _task, token, clients):
        await _agent(free_port, token, "idle", clients)
        watcher, _ = await _agent(free_port, token, "watcher", clients)
        await asyncio.sleep(4.0)
        frames = await _drain(watcher, timeout=0.2)
        assert [f for f in frames if f.get("op") == "peer_left"] == []
        assert "idle" in await _names(watcher, timeout=3.0)


async def test_messages_acknowledged_before_a_drop_reach_the_peer(
    tmp_data_dir, free_port, monkeypatch,
):
    """A message acknowledged to its sender is readable by the peer even when
    the peer is dropped afterwards. The drop aborts the connection, which
    discards what asyncio still buffers for it but not what the kernel
    holds, so a frame counts as written only once the kernel holds all of it.

    With asyncio's default 64 KiB write buffer, the medium and the small
    message were acknowledged while the peer read nothing, then discarded
    when the large one timed out and dropped the peer."""
    async with _serving(tmp_data_dir, free_port, monkeypatch) as (srv, _task, token, clients):
        stuck, stuck_sid = await _stuck_agent(srv, free_port, token, "stuck", clients)
        _, owner_sid = await _agent(free_port, token, "owner", clients, nonce="n-owner")
        sender, _ = await _agent(free_port, token, "", clients, role=shared.Role.CONTROL,
                                 for_session=owner_sid, nonce="n-owner")
        texts = [
            "S0-fits-the-socket-buffers",
            # Over the shrunk socket buffers, under 64 KiB.
            "M1-" + base64.b64encode(os.urandom(36000)).decode(),
            "S2-small",
            "L3-" + FILLER,
        ]
        acked = []
        for text in texts:
            await _send_op(sender, "send", to="stuck", text=text)
            if not await _errors_before_pong(sender, 2 * SEND_TIMEOUT_S + 2):
                acked.append(text[:2])
        assert stuck_sid not in srv._registry, "the peer was not dropped"

        stuck.transport.resume_reading()
        received = [text[:2] for text in await _read_to_end(stuck, 3.0)]
        assert [k for k in acked if k not in received] == [], (acked, received)
        # Not vacuous: a frame the kernel holds is acknowledged and still
        # arrives after the drop.
        assert "S0" in acked and "S0" in received


async def test_direct_message_that_waited_past_the_bound_is_not_delivered(
    tmp_data_dir, free_port, monkeypatch,
):
    """The send timeout bounds a direct message's wait in the recipient's
    queue as well as its write. The recipient here is slow, not stuck: it
    takes each frame ahead of the message within the bound and stays
    connected, but the message waits behind them for longer than the bound.
    The sender gets delivery_timeout, the message is never written, and the
    next message to the same peer is delivered."""
    send_timeout = 3.0
    async with _serving(tmp_data_dir, free_port, monkeypatch,
                        INTER_SESSION_SEND_TIMEOUT_S=str(send_timeout)) as (
            srv, _task, token, clients):
        slow, slow_sid = await _stuck_agent(srv, free_port, token, "slow", clients)
        filler, _ = await _agent(free_port, token, "filler", clients)
        _, owner_sid = await _agent(free_port, token, "owner", clients, nonce="n-owner")
        sender, _ = await _agent(free_port, token, "", clients, role=shared.Role.CONTROL,
                                 for_session=owner_sid, nonce="n-owner")
        received: list = []
        taken: asyncio.Queue = asyncio.Queue()

        async def read_one_at_a_time():
            # Stops reading after each message until the test resumes it.
            with contextlib.suppress(websockets.ConnectionClosed):
                async for raw in slow:
                    frame = json.loads(raw)
                    if frame.get("op") == "msg":
                        slow.transport.pause_reading()
                        received.append(frame["text"])
                        taken.put_nowait(frame["text"][:2])

        reader = asyncio.create_task(read_one_at_a_time())
        try:
            # Broadcasts do not expire. Each is over the socket buffers, so
            # its write lasts until the reader takes it.
            chunk = base64.b64encode(os.urandom(190 * 1024)).decode()
            for i in range(3):
                await _send_op(filler, "broadcast", text=f"B{i}-{chunk}")
            # Handled in order: the pong means all three are queued.
            await _send_op(filler, "ping")
            assert (await _recv_until(filler, "pong", timeout=PROMPT_S))["op"] == "pong"
            t0 = time.monotonic()
            await _send_op(sender, "send", to="slow", text="D-late")
            outcome = asyncio.create_task(
                _errors_before_pong(sender, 2 * send_timeout + 2))

            # Each broadcast is taken after half the bound: about 1.5 bounds
            # in all, so the direct message behind them expires while every
            # write stays within its own bound.
            for i in range(3):
                await asyncio.sleep(send_timeout / 2)
                slow.transport.resume_reading()
                assert await asyncio.wait_for(taken.get(), send_timeout) == f"B{i}"
            errors = await outcome
            assert time.monotonic() - t0 < 2 * send_timeout + 1
            assert [e["code"] for e in errors] == [shared.ErrorCode.DELIVERY_TIMEOUT], errors
            assert "waited" in errors[0]["message"]
            assert "not delivered" in errors[0]["message"]

            # The recipient stays registered and gets the next message.
            assert slow_sid in srv._registry
            assert "slow" in await _names(sender)
            await _send_op(sender, "send", to="slow", text="E-next")
            assert await _errors_before_pong(sender, 2 * send_timeout + 2) == []
            slow.transport.resume_reading()
            assert await asyncio.wait_for(taken.get(), send_timeout) == "E-"
            assert not [t for t in received if t.startswith("D-")], received
        finally:
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
