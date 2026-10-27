#!/bin/bash
# test_queue_coalesce.sh - regression test for the queue-coalescing
# character-loss bug found via tests/e2e/test_message_while_rate_limited.py.
#
# scripts/listener.sh's drain_queue_if_idle() used to coalesce queued
# messages with `tr '\x1e' '\n'`. BSD tr (macOS) has no \xHH
# hex-escape support: it parsed '\x1e' as the four LITERAL characters
# \, x, 1, e and mapped ANY of them to a newline, silently eating every
# letter 'e' (the most common letter in English) out of every queued
# message that went through this path.
#
# Fixed by using bash's own ${VAR//pat/repl} parameter expansion (the same
# technique listener.sh already used safely elsewhere for the identical
# substitution). This test extracts the REAL current coalescing snippet
# out of listener.sh (not a reimplementation) and runs it against
# e/x/1/backslash-heavy input, so it stays a true regression test tied to
# the actual file content, not a copy that can drift out of sync.
#
# Run: bash tests/test_queue_coalesce.sh

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/lib_assert.sh"

SNIPPET=$(awk '
  /# Coalesce one message per physical line/ { grab=1 }
  grab { print }
  grab && /combined="\$\{combined/ { exit }
' "$REPO_ROOT/scripts/listener.sh")

if [[ -z "$SNIPPET" ]]; then
  _fail "extract coalescing snippet from listener.sh" \
    "sed pattern matched nothing, listener.sh's coalescing code moved or changed shape; update this test's extraction pattern"
  finish
  exit $?
fi
_pass "extract coalescing snippet from listener.sh"

# Run the extracted snippet via a temp script FILE (not inline `bash -c
# "$SNIPPET"` string interpolation): embedding a multi-line snippet that
# itself contains $(...) / ${...} into another double-quoted bash -c
# string causes the OUTER shell to prematurely expand those sequences
# while constructing the string, before the inner bash ever sees them,
# silently evaluating against variables that don't exist yet. A real file
# sidesteps that entirely.
RUNNER=$(mktemp)
trap 'rm -f "$RUNNER"' EXIT

run_coalesce() {
  local draining="$1"
  {
    echo 'draining="$1"'
    printf '%s\n' "$SNIPPET"
    echo 'printf "%s" "$combined"'
  } > "$RUNNER"
  bash "$RUNNER" "$draining"
}

# Case 1: the scenario that surfaced the bug: two queued
# messages, both containing the letter 'e' repeatedly.
draining=$(mktemp)
printf 'first message, will get rate-limited\nsecond message sent mid-hold\n' > "$draining"
got=$(run_coalesce "$draining")
rm -f "$draining"
assert_contains "letter 'e' survives coalescing" "$got" "message"
assert_contains "letter 'e' survives coalescing (2)" "$got" "second"
assert_contains "letter 'e' survives coalescing (3)" "$got" "sent"
assert_not_contains "no stray blank-line-per-e corruption" "$got" $'m\nssag'

# Case 2: every character in the SET1 bug (\, x, 1, e) present in one
# message, to catch a regression in any of the four, not just 'e'.
draining=$(mktemp)
printf 'contains e and x and 1 and a backslash \\ here\n' > "$draining"
got=$(run_coalesce "$draining")
rm -f "$draining"
assert_eq "all four SET1-bug characters survive intact" \
  "contains e and x and 1 and a backslash \\ here" "$got"

# Case 3: an embedded newline (the feature \x1e is ACTUALLY meant to
# encode) must still correctly become a real newline, not get lost too.
draining=$(mktemp)
printf 'line one\x1eline two\n' > "$draining"
got=$(run_coalesce "$draining")
rm -f "$draining"
assert_eq "embedded newline (the real \\x1e use case) still restored" \
  "$(printf 'line one\nline two')" "$got"

finish
