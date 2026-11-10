"""A helper resolves only to the listener of its own Claude Code session.

discover.py walks up the process tree from the helper and reads each
ancestor's `<pid>.session`. The walk ends at the nearest process that is
certainly Claude Code: a session started from another session's shell
(`claude -p` run by a Bash tool) that has no listener of its own is "not
connected", never the outer session's listener. With no such process in
the tree the walk runs to the root, as before.

Process trees are built from stand-ins (tests/proctree.py), not from real
Claude Code sessions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from bin import shared
from tests.proctree import (
    BIN_DIR,
    run_probe,
    standins,  # noqa: F401  (fixture)
    kill_chain,
    start_runner,
    wait_chain,
    write_state,
)
from tests.test_helpers import _kill_server, _spawn_listener, _wait_for_state


class TestCcKind:
    """The classification find_cc_ancestor_pid() applies to one process."""

    STRONG = [
        ("claude", "/home/u/.local/share/claude/versions/2.1.3", ["claude", "--resume"]),
        ("2.1.3", "/home/u/.local/share/claude/versions/2.1.3",
         ["/home/u/.local/share/claude/versions/2.1.3"]),
        # only the executable path shows it: name and argv[0] are the version
        ("2.1.3", "/home/u/.local/share/claude/versions/2.1.3", ["2.1.3"]),
        ("claude", "/usr/bin/x", ["2.1.119"]),
        ("bun", "/home/u/.bun/bin/bun",
         ["bun", "/home/u/node_modules/@anthropic-ai/claude-code/cli.js", "-p", "x"]),
        ("node", "/usr/bin/node",
         ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]),
        ("node", "/usr/bin/node",
         ["node", "--no-warnings", "/usr/local/bin/claude", "-p", "x"]),
        ("node", "/usr/bin/node",
         ["node", "/home/u/.claude/local/node_modules/.bin/claude"]),
    ]
    WEAK = [
        ("node", "/usr/bin/node", ["node", "/home/u/.claude/hooks/notify.js"]),
        ("node", "/usr/bin/node", ["node", "/home/u/projects/claude-tool/server.js"]),
        ("claude-x", "/opt/claude-x", ["claude-x"]),
    ]
    NONE = [
        ("bash", "/usr/bin/bash", ["/bin/bash", "-c", "claude -p hi"]),
        ("python3", "/usr/bin/python3", ["python3", "/x/claude/versions.py"]),
        ("node", "/usr/bin/node", ["node", "/srv/app.js"]),
        ("", "", []),
    ]

    @pytest.mark.parametrize("name,exe,cmdline", STRONG)
    def test_claude_code_forms_are_strong(self, name, exe, cmdline):
        assert shared._cc_kind(name, exe, cmdline) == shared.CC_CERTAIN

    @pytest.mark.parametrize("name,exe,cmdline", WEAK)
    def test_loose_matches_stay_loose(self, name, exe, cmdline):
        assert shared._cc_kind(name, exe, cmdline) == shared.CC_LOOSE

    @pytest.mark.parametrize("name,exe,cmdline", NONE)
    def test_other_processes(self, name, exe, cmdline):
        assert shared._cc_kind(name, exe, cmdline) == 0


class TestIdentification:
    """find_cc_ancestor_pid() recognises each form a Claude Code process
    runs in, when that process is the helper's ancestor."""

    @pytest.mark.parametrize("form", [
        "claude", "claude-renamed", "versions", "node-pkg", "node-link"])
    def test_each_form_is_found(self, standins, tmp_data_dir, form):
        r = run_probe(standins, [standins[form], standins["sh"]])
        cc = r["chain"][0]
        assert r["cc"] == cc, r
        assert r["cc_strict"] == cc, r

    def test_loose_match_is_key_but_not_boundary(self, standins, tmp_data_dir):
        r = run_probe(standins, [standins["claude"], standins["sh"],
                                 standins["node-weak"], standins["sh"]])
        outer, _, weak, _ = r["chain"]
        assert r["cc"] == weak, r          # nearest match keys as before
        assert r["cc_strict"] == outer, r  # but only certain ones end the walk


class TestNestedSession:
    def test_nested_session_without_listener_is_not_connected(
            self, standins, tmp_data_dir):
        """outer claude (listener) -> sh -> inner claude (none) -> sh -> helper"""
        r = run_probe(standins,
                      [standins["claude"], standins["sh"],
                       standins["claude"], standins["sh"]],
                      prepare=lambda c: write_state(c[0], "outer"))
        assert r["chain"][2] == r["cc"], r
        assert r["state"] is None, f"resolved to the outer listener: {r}"

    @pytest.mark.parametrize("form", ["versions", "claude-renamed", "node-pkg",
                                      "node-link"])
    def test_inner_session_in_each_form(self, standins, tmp_data_dir, form):
        r = run_probe(standins,
                      [standins["claude"], standins["sh"],
                       standins[form], standins["sh"]],
                      prepare=lambda c: write_state(c[0], "outer"))
        assert r["state"] is None, f"resolved to the outer listener: {r}"

    def test_inner_session_with_own_listener(self, standins, tmp_data_dir):
        def prep(c):
            write_state(c[0], "outer")
            write_state(c[2], "inner")
        r = run_probe(standins,
                      [standins["claude"], standins["sh"],
                       standins["claude"], standins["sh"]], prepare=prep)
        assert r["state"] == "inner", r

    def test_listener_below_the_session_is_found(self, standins, tmp_data_dir):
        """A listener keyed by a process between the helper and its session
        (an older client keyed by its parent shell) is still on the walk."""
        def prep(c):
            write_state(c[0], "outer")
            write_state(c[3], "shell-keyed")
        r = run_probe(standins,
                      [standins["claude"], standins["sh"],
                       standins["claude"], standins["sh"], standins["sh"]],
                      prepare=prep)
        assert r["state"] == "shell-keyed", r


class TestUnchanged:
    def test_normal_case(self, standins, tmp_data_dir):
        """claude (listener) -> sh -> helper: the session's own listener."""
        r = run_probe(standins, [standins["claude"], standins["sh"]],
                      prepare=lambda c: write_state(c[0], "outer"))
        assert r["key"] == r["chain"][0], r
        assert r["state"] == "outer", r

    def test_explicit_override_wins(self, standins, tmp_data_dir):
        """INTER_SESSION_PPID_OVERRIDE names the listener key outright, even
        one above the nearest session."""
        # The override must be in the probe's environment before it starts,
        # so key the listener to a fixed pid instead of the chain's.
        write_state(424242, "overridden")
        r = run_probe(standins,
                      [standins["claude"], standins["sh"],
                       standins["claude"], standins["sh"]],
                      prepare=lambda c: write_state(c[0], "outer"),
                      env_extra={"INTER_SESSION_PPID_OVERRIDE": "424242"})
        assert r["key"] == 424242, r
        assert r["state"] == "overridden", r

    def test_loose_match_does_not_end_the_walk(self, standins, tmp_data_dir):
        """A process matched only by the loose rules (node under ~/.claude/,
        e.g. a hook) between helper and session: the walk goes on to the
        session's listener, as before."""
        r = run_probe(standins,
                      [standins["claude"], standins["sh"],
                       standins["node-weak"], standins["sh"]],
                      prepare=lambda c: write_state(c[0], "outer"))
        assert r["state"] == "outer", r

    def test_no_session_in_tree_walks_to_root(self, standins, tmp_data_dir):
        """No Claude Code process identifiable (cron, plain terminal): the
        walk is not cut short. Detached chain sh -> sh -> helper, listener
        keyed by the outer sh, which is neither the key nor the parent."""
        r = run_probe(standins, [standins["sh"], standins["sh"]],
                      prepare=lambda c: write_state(c[0], "plain"),
                      detached=True)
        assert r["cc"] == -1 and r["cc_strict"] == -1, (
            f"precondition: chain not detached from every Claude Code process: {r}")
        assert r["key"] == r["chain"][1], r
        assert r["state"] == "plain", r


def _stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)


def _spawn_owned_listener(port, name, data_dir, cc_pid):
    """A listener as bin/bus-inbox.py starts it: outside the session's
    process tree, keyed and owner-watched by the session's pid."""
    env = os.environ.copy()
    env["INTER_SESSION_DATA_DIR"] = str(data_dir)
    env["INTER_SESSION_PPID_OVERRIDE"] = str(cc_pid)
    env["INTER_SESSION_OWNER_PID"] = str(cc_pid)
    return subprocess.Popen(
        [sys.executable, str(BIN_DIR / "client.py"), "--port", str(port),
         "--name", name, "--idle-shutdown-minutes", "1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env, text=True)


def _helper_cmds(*cmds):
    return [[sys.executable, str(BIN_DIR / c[0]), *c[1:]] for c in cmds]


@pytest.mark.slow
class TestHelpersEndToEnd:
    """The CLI helpers against a server started here, from the helper
    position of a nested and of a normal session."""

    def _run(self, standins, tmp_data_dir, free_port, levels, cmds):
        peer = _spawn_listener(free_port, "peer", tmp_data_dir, 72002)
        proc, pids_file, go, out = start_runner(standins, levels, _helper_cmds(*cmds))
        outer = None
        try:
            chain = wait_chain(pids_file, len(levels))
            outer = _spawn_owned_listener(free_port, "outer", tmp_data_dir, chain[0])
            assert _wait_for_state(tmp_data_dir, chain[0]) is not None
            assert _wait_for_state(tmp_data_dir, 72002) is not None
            time.sleep(0.3)
            go.touch()
            proc.wait(timeout=60)
            res = json.loads(out.read_text())
            time.sleep(0.5)
            outer_alive = outer.poll() is None
            return res, outer_alive, peer
        finally:
            go.touch()
            kill_chain(proc)
            if outer is not None:
                _stop(outer)
            _stop(peer)
            _kill_server()

    def test_nested_helpers_do_not_act_for_outer_session(
            self, standins, tmp_data_dir, free_port):
        res, outer_alive, peer = self._run(
            standins, tmp_data_dir, free_port,
            [standins["claude"], standins["sh"], standins["claude"], standins["sh"]],
            [("list.py", "--self"),
             ("send.py", "--to", "peer", "--text", "from-nested"),
             ("disconnect.py",)])
        lst, snd, dis = res
        seen = {
            "list --self": (lst["rc"], lst["out"].strip().splitlines()[:1]),
            "send": (snd["rc"], snd["out"].strip(), "not connected" in snd["err"]),
            "disconnect": (dis["rc"], dis["out"].strip()),
            "outer listener alive": outer_alive,
        }
        assert seen == {
            "list --self": (0, ["not connected"]),
            "send": (1, "", True),
            "disconnect": (0, "not connected"),
            "outer listener alive": True,
        }

    def test_session_helpers_act_for_own_listener(
            self, standins, tmp_data_dir, free_port):
        res, outer_alive, peer = self._run(
            standins, tmp_data_dir, free_port,
            [standins["claude"], standins["sh"]],
            [("list.py", "--self"),
             ("send.py", "--to", "peer", "--text", "from-session"),
             ("disconnect.py",)])
        lst, snd, dis = res
        assert lst["rc"] == 0 and "name=outer" in lst["out"], lst
        assert snd["rc"] == 0 and snd["out"].strip() == "sent -> peer (12 chars)", snd
        assert dis["rc"] == 0 and dis["out"].strip().startswith("disconnected"), dis
        assert not outer_alive, "disconnect.py did not stop the session's own listener"
