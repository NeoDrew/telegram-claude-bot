#!/bin/bash
# tests/e2e/run.sh - run the E2E suite: the real listener.sh -> spawn_claude.sh
# -> stream_to_telegram.py pipeline, against fakes/fake_telegram_server.py and
# fakes/fake_claude.py instead of the real Telegram/Claude backends.
#
# Separate from tests/run.sh (the fast offline unit suite) because these spin
# up real subprocesses and take several seconds each rather than milliseconds.
# Run tests/run.sh on every change; run this before trusting a change to the
# listener/spawn/streamer interaction itself, or when chasing a production
# bug (rate limits, crash recovery, queueing) you want to reproduce safely.
#
# Usage:
#   bash tests/e2e/run.sh              # run every scenario
#   KEEP_SANDBOX=1 bash tests/e2e/run.sh   # keep the sandbox dir(s) on
#                                            failure for postmortem (path is
#                                            printed to stderr)

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

# Scenarios run in parallel (E2E_JOBS at a time, default 4): each one already
# gets its own sandbox copy of the repo and an OS-assigned port, so they never
# share state. Serially the suite took about 7 minutes. Output is printed per
# scenario once it finishes, so logs never interleave.
JOBS="${E2E_JOBS:-4}"
# Parallel load slows every scenario down, so give each wait more room.
export E2E_TIMEOUT_SCALE="${E2E_TIMEOUT_SCALE:-$([[ $JOBS -gt 1 ]] && echo 2 || echo 1)}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

export PY OUT
# shellcheck disable=SC2016
ls "$HERE"/test_*.py | xargs -P "$JOBS" -I{} bash -c '
  name="$(basename "$1" .py)"
  if "$PY" "$1" > "$OUT/$name.log" 2>&1; then echo PASS > "$OUT/$name.status"
  else echo FAIL > "$OUT/$name.status"; fi' _ {}

FAIL=0
for t in "$HERE"/test_*.py; do
  name="$(basename "$t" .py)"
  echo "=== $name ==="
  cat "$OUT/$name.log"
  status="$(cat "$OUT/$name.status" 2>/dev/null || echo FAIL)"
  echo "$status $name"
  [[ "$status" == "PASS" ]] || FAIL=1
  echo
done

if [[ "$FAIL" -eq 0 ]]; then
  echo "ALL E2E SCENARIOS PASSED"
else
  echo "E2E SUITE FAILED"
  grep -l FAIL "$OUT"/*.status 2>/dev/null | sed 's|.*/||; s|\.status$||; s|^|  failed: |'
fi
