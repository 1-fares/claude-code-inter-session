"""Tests for bin/auto_start.py — the /inter-session auto-start helper."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "inter-session" / "bin" / "auto_start.py"
ALWAYS = "always"
LAZY = "on-skill-invoke:inter-session"


@pytest.fixture
def fake_plugin_root(tmp_path: Path) -> Path:
    monitors_dir = tmp_path / "monitors"
    monitors_dir.mkdir()
    (monitors_dir / "monitors.json").write_text(json.dumps([
        {
            "name": "inter-session-client",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/skills/inter-session/bin/client.py",
            "description": "inter-session messages",
            "when": LAZY,
        }
    ], indent=2) + "\n")
    return tmp_path


def _run(args: list[str], plugin_root: Path | None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin"}
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


class TestStatus:
    def test_lazy_default(self, fake_plugin_root: Path):
        r = _run(["--status"], fake_plugin_root)
        assert r.returncode == 0
        assert "OFF" in r.stdout
        assert LAZY in r.stdout

    def test_after_set_on(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = ALWAYS
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        assert "ON" in r.stdout

    def test_custom_value(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = "on-skill-invoke:other-skill"
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        assert "CUSTOM" in r.stdout


class TestSet:
    def test_on_writes_always(self, fake_plugin_root: Path):
        r = _run(["--on"], fake_plugin_root)
        assert r.returncode == 0
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == ALWAYS

    def test_off_writes_lazy(self, fake_plugin_root: Path):
        # First flip to ON so we can verify OFF is a real change
        _run(["--on"], fake_plugin_root)
        r = _run(["--off"], fake_plugin_root)
        assert r.returncode == 0
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == LAZY

    def test_no_change_when_already_target(self, fake_plugin_root: Path):
        r = _run(["--off"], fake_plugin_root)
        assert r.returncode == 0
        assert "no change" in r.stdout
        # File contents preserved
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == LAZY

    def test_preserves_other_monitor_fields(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        before = json.loads(m.read_text())[0]
        _run(["--on"], fake_plugin_root)
        after = json.loads(m.read_text())[0]
        for k in ("name", "command", "description"):
            assert before[k] == after[k]

    def test_reload_instruction_printed(self, fake_plugin_root: Path):
        r = _run(["--on"], fake_plugin_root)
        assert "/reload-plugins" in r.stdout or "new Claude Code session" in r.stdout


class TestErrors:
    def test_no_env_falls_back_to_script_relative(self):
        """Without CLAUDE_PLUGIN_ROOT, the script self-locates from
        __file__ (bin/auto_start.py → repo root → monitors/monitors.json).
        It should succeed against the real repo."""
        r = _run(["--status"], plugin_root=None)
        assert r.returncode == 0
        assert "auto-start:" in r.stdout

    def test_env_root_takes_precedence(self, fake_plugin_root: Path):
        """When CLAUDE_PLUGIN_ROOT IS set, prefer it over script-relative."""
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = ALWAYS  # set the fake to ALWAYS
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        # Should reflect the fake's value, not the real repo's.
        assert "ON" in r.stdout

    def test_missing_monitor_entry(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        m.write_text(json.dumps([{"name": "other-monitor", "when": "always"}]))
        r = _run(["--status"], fake_plugin_root)
        assert r.returncode == 2
        assert "inter-session-client" in r.stderr

    def test_requires_one_of(self, fake_plugin_root: Path):
        # No flags → argparse should fail (mutually exclusive group, required=True)
        r = _run([], fake_plugin_root)
        assert r.returncode != 0


class TestStandaloneSkillDetection:
    def test_skills_path_is_standalone(self):
        from bin import auto_start
        p = Path.home() / ".claude" / "skills" / "is" / "bin" / "auto_start.py"
        assert auto_start._invoked_as_standalone_skill(p) is True

    def test_repo_path_is_not_standalone(self):
        from bin import auto_start
        p = Path("/home/x/projects/repo/skills/inter-session/bin/auto_start.py")
        assert auto_start._invoked_as_standalone_skill(p) is False

    def test_standalone_invocation_is_noop(self, tmp_path: Path):
        # A copy living under $HOME/.claude/skills/... must report a no-op and
        # touch no monitors.json, instead of silently editing the dev repo's.
        skill_bin = tmp_path / ".claude" / "skills" / "is" / "bin"
        skill_bin.mkdir(parents=True)
        copied = skill_bin / "auto_start.py"
        copied.write_text(SCRIPT.read_text())
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
        r = subprocess.run(
            [sys.executable, str(copied), "--on"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0
        assert "plugin-only" in r.stdout

    def test_plugin_root_env_overrides_standalone(self, tmp_path: Path, fake_plugin_root: Path):
        # An explicit CLAUDE_PLUGIN_ROOT means a real plugin/dev context: even
        # when invoked from under ~/.claude/skills, proceed and edit it.
        skill_bin = tmp_path / ".claude" / "skills" / "is" / "bin"
        skill_bin.mkdir(parents=True)
        copied = skill_bin / "auto_start.py"
        copied.write_text(SCRIPT.read_text())
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
               "CLAUDE_PLUGIN_ROOT": str(fake_plugin_root)}
        r = subprocess.run(
            [sys.executable, str(copied), "--on"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0
        assert "plugin-only" not in r.stdout
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == ALWAYS
