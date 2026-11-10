"""Stand-in process trees for the Claude Code ancestor tests.

A stand-in is a copy of bash that carries one property by which
shared.find_cc_ancestor_pid() recognises a Claude Code process: the
process name `claude`, an executable under a `claude/versions/`
directory, or node running the @anthropic-ai/claude-code package. Each
level of a chain runs `level.sh`, which records its pid and runs the next
level as a child, so the probe at the leaf sees the whole chain as its
ancestors. No real Claude Code session is involved.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "inter-session"
BIN_DIR = SKILL_DIR / "bin"

_LEVEL_SH = '#!/bin/sh\necho $$ >> "$CHAIN_PIDS"\n"$@"\nexit $?\n'

# The leaf: report what this process resolves, as JSON in $PROBE_OUT.
_PROBE_PY = r'''
import json, os, sys, time
end = time.monotonic() + 30
while not os.path.exists(os.environ["GO_FILE"]) and time.monotonic() < end:
    time.sleep(0.05)
sys.path.insert(0, sys.argv[1])
from bin import discover, shared
out = {"pid": os.getpid(), "ppid": os.getppid(),
       "cc": shared.find_cc_ancestor_pid(),
       "key": shared.resolve_listener_key()}
try:
    out["cc_strict"] = shared.find_cc_ancestor_pid(strict=True)
except TypeError:
    out["cc_strict"] = None
if len(sys.argv) > 2 and sys.argv[2] == "owner":
    from bin import client
    out["owner_pid"] = client.Client(name="x").owner_pid
state = discover.find_listener_state()
out["state"] = state.get("name") if state else None
tmp = os.environ["PROBE_OUT"] + ".tmp"
with open(tmp, "w") as f:
    json.dump(out, f)
os.replace(tmp, os.environ["PROBE_OUT"])
'''

# The leaf for end-to-end runs: wait for $GO_FILE, run each helper command
# in $RUN_CMDS, write their results to $RUN_OUT.
_RUNNER_PY = r'''
import json, os, subprocess, sys, time
end = time.monotonic() + 30
while not os.path.exists(os.environ["GO_FILE"]) and time.monotonic() < end:
    time.sleep(0.1)
res = []
for cmd in json.loads(os.environ["RUN_CMDS"]):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    res.append({"rc": r.returncode, "out": r.stdout, "err": r.stderr})
tmp = os.environ["RUN_OUT"] + ".tmp"
with open(tmp, "w") as f:
    json.dump(res, f)
os.replace(tmp, os.environ["RUN_OUT"])
'''


def _copy_exe(src: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(0o755)
    return dest


def _copy_script(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(0o755)
    return dest


@pytest.fixture(scope="session")
def standins(tmp_path_factory):
    """Level specs (executable, argv[0], script) for chain building."""
    root = tmp_path_factory.mktemp("standins")
    bash = shutil.which("bash") or "/bin/bash"
    level = root / "level.sh"
    level.write_text(_LEVEL_SH)
    level.chmod(0o755)
    (root / "probe.py").write_text(_PROBE_PY)
    (root / "runner.py").write_text(_RUNNER_PY)
    claude = _copy_exe(bash, root / "bin" / "claude")
    versions = _copy_exe(bash, root / "share" / "claude" / "versions" / "9.9.9")
    node = _copy_exe(bash, root / "nodebin" / "node")
    pkg = _copy_script(level, root / "lib" / "node_modules" / "@anthropic-ai"
                       / "claude-code" / "cli.js")
    link = _copy_script(level, root / "npmbin" / "claude")
    hook = _copy_script(level, root / "home" / ".claude" / "hooks" / "hook.js")
    return {
        "root": root,
        # argv[0] `claude` and process name `claude`
        "claude": (str(claude), "claude", str(level)),
        # process name `claude` only (argv[0] is a version string)
        "claude-renamed": (str(claude), "2.1.119", str(level)),
        # executable under claude/versions/, name and argv[0] `9.9.9`
        "versions": (str(versions), "9.9.9", str(level)),
        # node running the package's cli.js
        "node-pkg": (str(node), "node", str(pkg)),
        # node running the package through its npm bin link `claude`
        "node-link": (str(node), "node", str(link)),
        # node running a script under ~/.claude/: matched only by the
        # loose rules, never certainly Claude Code
        "node-weak": (str(node), "node", str(hook)),
        "sh": ("/bin/sh", "sh", str(level)),
    }


def chain_argv(levels, leaf_argv):
    """argv that runs `leaf_argv` under `levels` (outermost first). Each
    level is exec'd with its own argv[0] through a bash trampoline that
    replaces itself, so it adds no process to the chain."""
    argv = list(leaf_argv)
    for exe, argv0, script in reversed(levels):
        argv = ["/bin/bash", "-c", 'exec -a "$0" "$@"', argv0, exe, script, *argv]
    return argv


def chain_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    for k in ("INTER_SESSION_PPID_OVERRIDE", "INTER_SESSION_OWNER_PID",
              "BASH_ENV", "ENV"):
        env.pop(k, None)
    env["INTER_SESSION_NO_REEXEC"] = "1"
    if extra:
        env.update(extra)
    return env


def _wait_file(path: Path, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


def run_probe(standins, levels, *, prepare=None, env_extra=None,
              detached=False, owner=False, timeout=20.0) -> dict:
    """Run probe.py under `levels`; return its report plus `chain`, the
    level pids outermost first. `prepare(chain)` runs once every level is
    up and before the probe resolves anything (to write state files keyed
    by those pids). `detached` starts the chain in a new session whose
    launcher exits at once, so the chain is reparented away from this
    process and has no Claude Code ancestor."""
    root = standins["root"]
    tag = uuid.uuid4().hex[:8]
    pids_file = root / f"chain-{tag}.pids"
    go = root / f"go-{tag}"
    out = root / f"probe-{tag}.json"
    leaf = [sys.executable, str(root / "probe.py"), str(SKILL_DIR)]
    if owner:
        leaf.append("owner")
    argv = chain_argv(levels, leaf)
    env = chain_env({"CHAIN_PIDS": str(pids_file), "PROBE_OUT": str(out),
                     "GO_FILE": str(go), **(env_extra or {})})
    proc = None
    if detached:
        launcher = [sys.executable, "-c",
                    "import subprocess, sys; subprocess.Popen(sys.argv[1:], "
                    "start_new_session=True, stdin=subprocess.DEVNULL, "
                    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
                    *argv]
        subprocess.run(launcher, env=env, timeout=timeout, check=True)
    else:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    try:
        chain = wait_chain(pids_file, len(levels), timeout)
        if prepare is not None:
            prepare(chain)
        go.touch()
        if proc is not None:
            proc.wait(timeout=timeout)
        assert _wait_file(out, timeout), (
            "probe wrote no report"
            + (f": rc={proc.returncode} err={proc.stderr.read()!r}" if proc else ""))
    finally:
        go.touch()
        if proc is not None:
            kill_chain(proc)
    report = json.loads(out.read_text())
    report["chain"] = chain
    return report


def write_state(pid: int, name: str, listener_pid: int | None = None) -> Path:
    """A listener state file keyed by `pid`, as client.py writes it."""
    from bin import shared
    shared.secure_dir(shared.clients_dir())
    path = shared.client_session_path(pid)
    path.write_text(json.dumps({
        "session_id": str(uuid.uuid4()), "name": name, "label": "",
        "token": "t", "nonce": "n",
        "listener_pid": listener_pid if listener_pid is not None else os.getpid(),
        "host": "127.0.0.1", "port": 1,
    }))
    return path


def start_runner(standins, levels, cmds, *, env_extra=None):
    """Start runner.py under `levels` in the background. It waits for
    `go` to exist before it runs `cmds`. Returns (proc, pids_file, go, out)."""
    root = standins["root"]
    tag = uuid.uuid4().hex[:8]
    pids_file = root / f"chain-{tag}.pids"
    go = root / f"go-{tag}"
    out = root / f"run-{tag}.json"
    argv = chain_argv(levels, [sys.executable, str(root / "runner.py")])
    env = chain_env({"CHAIN_PIDS": str(pids_file), "GO_FILE": str(go),
                     "RUN_OUT": str(out), "RUN_CMDS": json.dumps(cmds),
                     **(env_extra or {})})
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    return proc, pids_file, go, out


def kill_chain(proc) -> None:
    """Kill a chain this module started, with every level and its leaf.

    The chain runs in its own session, so its outermost process leads a
    process group that holds all of it. Killing only that process left
    the levels and the leaf running until their own waits ended. The group
    is signalled only while its leader is alive and not yet reaped, so the
    pid cannot have been reused."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait(timeout=5)


def wait_chain(pids_file: Path, n: int, timeout: float = 10.0) -> list[int]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pids_file.exists():
            pids = [int(x) for x in pids_file.read_text().split()]
            if len(pids) >= n:
                return pids
        time.sleep(0.05)
    raise AssertionError(f"chain did not start {n} levels")
