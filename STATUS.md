# Status

Working notes for this fork. Transient status and lessons; stable
architecture lives in `CLAUDE.md`.

## Repository

- Private fork at `1-fares/claude-code-inter-session` (origin, SSH),
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

Caveats from that run:

- The file-pointer test was confounded by pre-existing `/tmp/is-demo`
  artifacts: the receiver ran an existing `sieve.py` rather than creating
  it from the spec. Delivery itself was still proven.
- **Log-recovery is unverified.** When a long message is sent inline and
  truncated, the receiver is supposed to fetch the full body from
  `messages.log` via the `cont` pointer. In the run the receiver instead
  recognized a repeat and replied from memory, so this path has not
  actually been exercised.
- Resending a task inline re-triggers execution; idempotency is the
  sender's responsibility.

## Open items

- Verify log-recovery with novel >400-char inline content whose
  actionable instruction sits past the cap.
- Optional: version bump; squash the multi-part commit and its revert for
  a cleaner history.

## Install / run

- Standalone: symlink the skill dir to `~/.claude/skills/is` → `/is`.
- Deps auto-install into `~/.claude/data/inter-session/venv` on first
  connect.
- `make test`: full suite passes except a few subprocess integration
  tests that fail in restricted sandboxes (psutil server-identity +
  process-tree discovery); they pass in a normal environment.
