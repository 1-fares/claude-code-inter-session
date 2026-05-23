"""Toggle the inter-session plugin monitor's auto-start behavior.

Edits the `when` field of the inter-session-client monitor in the
plugin's `monitors/monitors.json` atomically. The script self-locates
relative to its own path (no env var needed); CLAUDE_PLUGIN_ROOT is
honored as an override if set.

Modes:
  always                          start at every CC session open
  on-skill-invoke:inter-session   start when /inter-session is first invoked

Changes take effect on `/reload-plugins` or the next CC session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ALWAYS = "always"
LAZY = "on-skill-invoke:inter-session"
MONITOR_NAME = "inter-session-client"


def _resolve_monitors_path() -> Path:
    # monitors.json lives at <plugin-root>/monitors/monitors.json.
    # This script lives at <plugin-root>/skills/inter-session/bin/auto_start.py,
    # so the plugin root is FOUR parents up from this file.
    #
    # Resolution order:
    #   1. CLAUDE_PLUGIN_ROOT env var (override; rarely set in subprocesses
    #      because ${CLAUDE_PLUGIN_ROOT} in CC manifests is a substitution
    #      token, not an exported env var).
    #   2. Script-relative: walk up four parents.
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parents[3])

    for root in candidates:
        p = root / "monitors" / "monitors.json"
        if p.is_file():
            return p

    sys.stderr.write(
        "auto_start: could not locate monitors.json. Searched: "
        f"{[str(c / 'monitors' / 'monitors.json') for c in candidates]}\n"
    )
    sys.exit(2)


def _load(path: Path) -> list:
    monitors = json.loads(path.read_text())
    if not isinstance(monitors, list):
        sys.stderr.write("auto_start: monitors.json must be a JSON list\n")
        sys.exit(2)
    return monitors


def _find_entry(monitors: list) -> dict:
    for m in monitors:
        if isinstance(m, dict) and m.get("name") == MONITOR_NAME:
            return m
    sys.stderr.write(
        f"auto_start: no monitor named {MONITOR_NAME!r} found in monitors.json\n"
    )
    sys.exit(2)


def _atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".monitors.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_status() -> int:
    path = _resolve_monitors_path()
    entry = _find_entry(_load(path))
    when = entry.get("when", "always")
    if when == ALWAYS:
        label = "ON  (auto-start at every session)"
    elif when == LAZY:
        label = "OFF (lazy: starts on first /inter-session invocation)"
    else:
        label = f"CUSTOM ({when})"
    print(f"auto-start: {label}")
    print(f"  when: {when}")
    print(f"  file: {path}")
    return 0


def cmd_set(target: str) -> int:
    path = _resolve_monitors_path()
    monitors = _load(path)
    entry = _find_entry(monitors)
    prev = entry.get("when", "always")
    if prev == target:
        print(f"auto-start: already {target!r}; no change")
        return 0
    entry["when"] = target
    _atomic_write(path, json.dumps(monitors, indent=2) + "\n")
    print(f"auto-start: {prev!r} -> {target!r}")
    print("Reload to apply: /reload-plugins (or open a new Claude Code session).")
    return 0


def _invoked_as_standalone_skill(invoked: Path | None = None) -> bool:
    """True when invoked from a standalone-skill install
    (`~/.claude/skills/<name>/`), where Claude Code never reads
    `monitors.json`, so auto-start has no effect. Keys on the *invocation*
    path (unresolved `__file__`), not its resolved symlink target, which for a
    symlinked dev install points at the repo. Plugin and `--plugin-dir`
    installs live elsewhere and return False."""
    if invoked is None:
        try:
            invoked = Path(__file__)
        except NameError:
            return False
    skills_root = Path.home() / ".claude" / "skills"
    return skills_root in invoked.parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="print current setting")
    g.add_argument("--on", action="store_true", help=f'set when="{ALWAYS}"')
    g.add_argument("--off", action="store_true", help=f'set when="{LAZY}"')
    args = parser.parse_args(argv)

    # auto-start only affects plugin installs (Claude Code reads monitors.json
    # there). In a standalone-skill install it does nothing, and editing the
    # repo's monitors.json via the symlink would silently mislead. Bail with a
    # clear message. An explicit CLAUDE_PLUGIN_ROOT means a real plugin/dev
    # context, so honor it.
    if not os.environ.get("CLAUDE_PLUGIN_ROOT") and _invoked_as_standalone_skill():
        print(
            "auto-start is a plugin-only feature and has no effect here: this is "
            "a standalone-skill install (~/.claude/skills/...), where Claude Code "
            "does not read monitors.json. The monitor starts when you first run "
            "/is in a session; there's nothing to toggle."
        )
        return 0

    if args.status:
        return cmd_status()
    if args.on:
        return cmd_set(ALWAYS)
    if args.off:
        return cmd_set(LAZY)
    return 2  # unreachable due to required=True


if __name__ == "__main__":
    raise SystemExit(main())
