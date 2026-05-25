# Status

Working notes for this fork. Transient status and lessons; stable
architecture lives in `CLAUDE.md`.

## Repository

- Public fork at `1-fares/claude-code-inter-session` (origin, SSH),
  disconnected from the original upstream. Single remote, so commit +
  push lands directly here.

## Changes in this line of work

- **Long messages use a file pointer.** `send.py --file <path>` sends a
  short pointer (`read and execute the task in the file at <abs>`); the
  receiver opens the file in full, sidestepping the ~400-char stdout
  notification cap. The earlier multi-part (`part=i/N`) approach was
  reverted.
- **Short invocation `/is`.** A standalone-skill install takes its slash
  trigger from the install directory name, so symlinking the skill dir as
  `~/.claude/skills/is` gives `/is`. SKILL.md has short subcommand
  aliases (`c/l/s/b/r/st/d`).
- **Security guards left in place.** A single-user relaxation (drop the
  control-nonce check and the client-side server-identity verification)
  was tried, then reverted. Both guards are active.
- **auto-start** is now a clear no-op in standalone-skill mode instead of
  silently editing the repo's `monitors.json` through the symlink.

## Real-time delivery depends on the Monitor tool

- The receiver only reacts on its own when the `Monitor` tool is present
  (per-line push). Without it, `client.py` runs as a background task and
  delivery is **poll-based**: the receiver does not wake on an incoming
  message until something makes it read its inbox.
- `Monitor` is unavailable if **either** `DISABLE_TELEMETRY` or
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is set. Setting them to
  `"0"` is not enough, Claude Code treats a present variable as set, so
  they must be **absent** from settings/env. Enabling Monitor re-enables
  Anthropic operational metrics only (no code/paths); error reporting,
  surveys, and bug command stay off via their own flags.

## Test status

Push-based end-to-end run across two live sessions: 5/5 passed, verified
against the server's `messages.log` (list, short round-trip,
file-pointer delivery, 61-vs-632-byte contrast, destructive-request
guardrail returning `question:`).

A second run (fresh paths, novel content) closed the two earlier gaps,
both wire-verified against `messages.log`:

- **File-pointer from scratch:** in a freshly emptied dir the receiver
  created `/tmp/is-fresh/factorial.py` + `out.txt` from the spec and
  replied `done: 720`.
- **Log-recovery verified:** a 584-byte inline message with its
  actionable instruction at byte 547 (past the 400 cap, so clipped from
  the notification); the receiver replied `done: 54`, obtainable only by
  fetching the full body from `messages.log`.

Note: resending a task inline re-triggers execution; idempotency is the
sender's responsibility.

## Open items

- Core behavior is fully validated (push delivery, file-pointer from
  scratch, truncation log-recovery, safety guardrail). No functional gaps
  open.
- Optional: version bump; squash the multi-part commit and its revert for
  a cleaner history.

## Install / run

- Standalone: symlink the skill dir to `~/.claude/skills/is` → `/is`.
- Deps auto-install into `~/.claude/data/inter-session/venv` on first
  connect.
- `make test`: full suite passes except a few subprocess integration
  tests that fail in restricted sandboxes (psutil server-identity +
  process-tree discovery); they pass in a normal environment.
