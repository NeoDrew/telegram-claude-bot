#!/bin/bash
# test_windows_screencapture.sh - regression test for the "/windows menu
# option broken" bug.
#
# scripts/commands/windows.sh called the bare command name `screencapture`
# as its fallback whenever the primary Quartz capture path failed. The
# launchd plist's PATH (built by install.sh) omitted /usr/sbin, where
# screencapture actually lives, so the fallback silently died with
# "screencapture: command not found" every time the Quartz path also
# failed (e.g. missing Screen Recording permission), which is exactly the
# situation the fallback exists to cover. The symptom in
# logs/windows.log was windows enumerated but zero captured and zero sent.
#
# Fixed two ways: windows.sh now calls /usr/sbin/screencapture by absolute
# path, and install.sh's generated plist PATH now includes /usr/sbin too
# (defence in depth for any other bare macOS system binary a future
# handler might call).
#
# Run: bash tests/test_windows_screencapture.sh

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/lib_assert.sh"

# --- Case 1: windows.sh must never call bare `screencapture` again -------
# A plain string match, deliberately excluding the absolute-path form and
# comment lines, so this fails loudly if a future edit reintroduces the
# bare call (e.g. copy-pasting the old fallback line from history).
BARE_CALLS=$(grep -n '[^/]screencapture ' "$REPO_ROOT/scripts/commands/windows.sh" \
  | grep -v '/usr/sbin/screencapture' | grep -v '^\s*[0-9]*:\s*#')
if [[ -z "$BARE_CALLS" ]]; then
  _pass "windows.sh calls screencapture by absolute path, not bare"
else
  _fail "windows.sh calls screencapture by absolute path, not bare" \
    "found bare call(s):\n$BARE_CALLS"
fi

# --- Case 2: the actual absolute path used must be a real, executable file
if [[ -x /usr/sbin/screencapture ]]; then
  _pass "/usr/sbin/screencapture exists and is executable on this machine"
else
  _fail "/usr/sbin/screencapture exists and is executable on this machine" \
    "not found or not executable, windows.sh's absolute-path fix would still fail here"
fi

# --- Case 3: install.sh's generated launchd PATH includes /usr/sbin ------
GENERATED_PATH_LINE=$(grep -n '<key>PATH</key>' -A1 "$REPO_ROOT/scripts/install.sh" | tail -1)
if [[ "$GENERATED_PATH_LINE" == *"/usr/sbin"* ]]; then
  _pass "install.sh's generated plist PATH includes /usr/sbin"
else
  _fail "install.sh's generated plist PATH includes /usr/sbin" \
    "PATH line: $GENERATED_PATH_LINE"
fi

# --- Case 4: reproduce the exact bug conditions, prove the fix survives --
# A typical launchd plist PATH of the shape this bug happened under,
# WITHOUT /usr/sbin, so this test fails the same way the bot did if the
# fix ever regresses.
BUGGY_PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Sanity check: under the BUGGY path, the bare command must indeed be
# unreachable, so this test is actually reproducing the real failure mode
# and not just checking a tautology. Captured into a variable first
# (not piped directly into grep -q): with `set -o pipefail` active, piping
# a deliberately-failing command straight into grep makes the PIPELINE's
# exit status reflect the failing command, not grep's own match result,
# even when grep itself matches successfully.
BARE_OUTPUT=$(env -i PATH="$BUGGY_PATH" bash -c 'screencapture -l 0 -x -o -t png /tmp/nope.png' 2>&1)
if echo "$BARE_OUTPUT" | grep -q "command not found"; then
  _pass "sanity: bare screencapture is genuinely unreachable under the buggy PATH"
else
  _fail "sanity: bare screencapture is genuinely unreachable under the buggy PATH" \
    "expected 'command not found', got: $BARE_OUTPUT (if this machine's PATH assumptions changed, the case below may be a false pass)"
fi

# The actual fix: under the SAME buggy PATH, the ABSOLUTE-path form must
# still be found and invoked (it will still fail, window id 0/999999 is
# fake, but the failure must be a screencapture-level error, not a shell
# "command not found").
OUTPUT=$(env -i PATH="$BUGGY_PATH" bash -c \
  '/usr/sbin/screencapture -l 999999999 -x -o -t png /tmp/tcbot_test_nope.png' 2>&1)
rm -f /tmp/tcbot_test_nope.png
if echo "$OUTPUT" | grep -qi "command not found"; then
  _fail "absolute-path screencapture survives the buggy PATH" \
    "still got 'command not found' even via the absolute path: $OUTPUT"
else
  _pass "absolute-path screencapture survives the buggy PATH (found and invoked, unlike the bare form above)"
fi

finish
