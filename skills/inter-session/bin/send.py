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

import websockets

from bin import shared, discover


def _compose_text(text, file) -> str:
    """Build the message body to send.

    With --file, send a short pointer instructing the receiver to read the
    file directly (via its own Read tool) rather than streaming the body
    through the bus. This sidesteps the stdout notification cap entirely:
    the pointer is tiny, and the receiver gets the full content from the
    file. The path is resolved to an absolute path and must exist."""
    if file:
        abspath = Path(file).expanduser().resolve(strict=True)  # raises if missing
        if not abspath.is_file():
            raise ValueError(f"not a regular file: {abspath}")
        if text:
            return f"{text} (read the file at {abspath} and act on it)"
        return f"read and execute the task in the file at {abspath}"
    return text


async def _run(args) -> int:
    state, state_path = discover.find_listener_state_with_path()
    if state is None:
        print("not connected; run /inter-session in this Claude Code session first",
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
        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)
        if welcome.get("op") == "error":
            code = welcome.get("code", "")
            if code in (shared.ErrorCode.UNKNOWN_PEER, shared.ErrorCode.UNAUTHORIZED):
                # Stale state file: listener referenced no longer registered.
                # TOCTOU-safe: only unlink if the file still has the same
                # session_id+nonce as we read; otherwise a fresh listener has
                # written new state between our read and now.
                discover.unlink_if_matches(state_path, state)
                print("not connected; run /inter-session in this Claude Code session first",
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

        # Wait briefly for an error frame; success is silence.
        try:
            resp_raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            resp = json.loads(resp_raw)
            if resp.get("op") == "error":
                msg = f"error: {resp.get('code', '')}: {resp.get('message', '')}"
                if "matches" in resp:
                    msg += f" (matches: {', '.join(resp['matches'])})"
                if "candidates" in resp:
                    msg += f" (try: {', '.join(resp['candidates'])})"
                print(msg, file=sys.stderr)
                return 1
        except asyncio.TimeoutError:
            pass
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
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("dependencies missing — run /inter-session install-deps", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
