#!/usr/bin/env bash
# test_wakeup_rescue.sh - listener.sh's rescue_missed_wakeups delivers a
# ScheduleWakeup that Claude Code never fired (it skips wakeups while any
# background task runs), but only once, only when overdue past the grace
# period, and only while the room sits idle between turns.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REAL="$(cd "$HERE/.." && pwd)"
. "$HERE/lib_assert.sh"

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
FN="$T/fn.sh"
# Lift the function and its tunables straight out of the real listener.
awk '/^WAKEUP_GRACE_S=/{cap=1} cap{print} cap&&/^}$/{exit}' "$REAL/scripts/listener.sh" > "$FN"
[[ -s "$FN" ]] || { echo "FAIL: could not extract rescue_missed_wakeups"; exit 1; }

ROOMS_DIR="$T/rooms"; mkdir -p "$ROOMS_DIR/-1"
REPO_ROOT="$T/repo"; mkdir -p "$REPO_ROOT/scripts"
cat > "$REPO_ROOT/scripts/spawn_state.py" <<'PY'
import os
def classify(_p):
    return {"state": os.environ.get("FAKE_STATE", "idle")}
PY
RD="$ROOMS_DIR/-1"
BUSY=1
is_busy() { (( BUSY )); }
log() { :; }
emit_metric() { :; }
inject_to_inbox() { printf '%s\n' "$1" >> "$RD/injected"; }
. "$FN"
WAKEUP_GRACE_S=120
reset() { rm -f "$RD/injected" "$RD/.wakeup_due"; touch "$RD/.inbox_ready"; _last_wakeup_check_at=0; BUSY=1; export FAKE_STATE=idle; }
due() { printf '{"due": %s, "prompt": "%s"}' "$(( $(date +%s) + $1 ))" "$2" > "$RD/.wakeup_due"; }

reset; due -300 "/loop keep going"; rescue_missed_wakeups
assert_file_eq "overdue wakeup delivered verbatim (slash prompt)" "$RD/injected" "/loop keep going"
assert_no_file "delivered wakeup is consumed"                  "$RD/.wakeup_due"
_last_wakeup_check_at=0; rescue_missed_wakeups
assert_eq      "delivered only once"                           "$(wc -l < "$RD/injected" | tr -d ' ')" "1"

reset; due -60 "/loop keep going"; rescue_missed_wakeups
assert_no_file "within the grace period nothing is injected"   "$RD/injected"
assert_file_exists "within the grace period the wakeup is kept" "$RD/.wakeup_due"

reset; due -300 "/loop keep going"; FAKE_STATE=tool; rescue_missed_wakeups
assert_no_file "mid-turn room is left alone"                   "$RD/injected"

reset; due -300 "/loop keep going"; rm -f "$RD/.inbox_ready"; rescue_missed_wakeups
assert_no_file "room without a live inbox is left alone"       "$RD/injected"

reset; due -300 "/loop keep going"; BUSY=0; rescue_missed_wakeups
assert_no_file "dead spawn: nothing injected"                  "$RD/injected"
assert_no_file "dead spawn: stale wakeup dropped"              "$RD/.wakeup_due"

reset; due -300 "<<autonomous-loop-dynamic>>"; rescue_missed_wakeups
assert_contains "non-slash prompt gets an explanation"         "$(cat "$RD/injected")" "[Scheduled wakeup]"
assert_contains "non-slash prompt keeps the original prompt"   "$(cat "$RD/injected")" "<<autonomous-loop-dynamic>>"

reset; due -300 "/loop keep going"; _last_wakeup_check_at=$(date +%s); rescue_missed_wakeups
assert_no_file "check is throttled"                             "$RD/injected"

finish
