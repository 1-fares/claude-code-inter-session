"""The dependencies-missing notice reaches the user.

SKILL.md documents `[inter-session] dependencies missing` as the notice the
scripts print when their Python lacks the runtime deps (no venv yet, or after
the venv reset SKILL.md recommends). The entry points import websockets at
module level, so the check must run before that import, and after the venv
re-exec bootstrap so that an existing venv is still used. client.py runs
under Monitor, which reports only stdout; the control CLIs run under Bash and
keep stdout for their results.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent / "skills" / "inter-session" / "bin"

# script -> (stream that carries the notice, exit code)
SCRIPTS = {
    "client.py": ("stdout", 0),
    "list.py": ("stderr", 1),
    "send.py": ("stderr", 1),
    "disconnect.py": ("stderr", 1),
}

# HOME points at an empty temp dir, so skill_command() falls back to the
# plugin form.
NOTICE = ("[inter-session] dependencies missing: run "
          "/inter-session:inter-session install-deps "
          "(No module named 'websockets')\n")

SYSTEM_PYTHON = "/usr/bin/python3"


def _env(tmp_path: Path, *, reexec: bool = False) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    env["INTER_SESSION_DATA_DIR"] = str(tmp_path / "data")
    if reexec:
        env.pop("INTER_SESSION_NO_REEXEC", None)
    else:
        env["INTER_SESSION_NO_REEXEC"] = "1"
    return env


@pytest.fixture(params=["system-python3", "no-site"])
def python_without_websockets(request, tmp_path) -> list[str]:
    """Interpreter command whose sys.path has no websockets.

    system-python3 is the audit's method (the user's state after a venv
    reset); it is skipped where that interpreter is absent or has
    websockets. no-site runs the test interpreter with -E -S, which keeps
    every site-packages directory off sys.path on any machine.
    """
    if request.param == "system-python3":
        if sys.platform == "darwin" or not os.path.isfile(SYSTEM_PYTHON):
            pytest.skip(f"{SYSTEM_PYTHON} not usable here")
        cmd = [SYSTEM_PYTHON]
    else:
        cmd = [sys.executable, "-E", "-S"]
    probe = subprocess.run([*cmd, "-c", "import websockets"],
                           capture_output=True, env=_env(tmp_path), timeout=30)
    if probe.returncode == 0:
        if request.param == "system-python3":
            pytest.skip(f"{SYSTEM_PYTHON} has websockets installed")
        pytest.fail(f"{cmd} can import websockets")
    return cmd


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_notice_when_websockets_missing(script, python_without_websockets, tmp_path):
    stream, code = SCRIPTS[script]
    r = subprocess.run(
        [*python_without_websockets, str(BIN_DIR / script), "--help"],
        capture_output=True, text=True, env=_env(tmp_path), timeout=30,
    )
    out = {"stdout": r.stdout, "stderr": r.stderr}
    other = "stderr" if stream == "stdout" else "stdout"
    assert out[stream] == NOTICE, out
    assert out[other] == "", out
    assert r.returncode == code


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_venv_reexec_runs_before_the_check(script, tmp_path):
    """With the venv present, the script re-execs into it instead of
    printing the notice."""
    venv_bin = tmp_path / "home" / ".claude" / "data" / "inter-session" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_py = venv_bin / "python"
    # Stand-in venv interpreter: the test Python, which has the deps.
    # INTER_SESSION_NO_REEXEC stops the second process from re-execing
    # again, since its sys.prefix is not the stand-in venv.
    venv_py.write_text(
        f'#!/bin/sh\nINTER_SESSION_NO_REEXEC=1 exec "{sys.executable}" "$@"\n'
    )
    venv_py.chmod(0o755)
    r = subprocess.run(
        [sys.executable, "-E", "-S", str(BIN_DIR / script), "--help"],
        capture_output=True, text=True, env=_env(tmp_path, reexec=True),
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("usage:"), r.stdout
    assert "dependencies missing" not in r.stdout + r.stderr
