#!/bin/bash
# tests/run.sh - run the whole offline test suite. Exit 0 iff all pass.
# No network, no running bot, no real room state touched. Safe to run any
# time, including from a pre-commit hook.
#
#   bash tests/run.sh            # run everything
#   bash tests/run.sh -q         # quiet: only suite-level PASS/FAIL lines
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"
QUIET=0; [[ "${1:-}" == "-q" ]] && QUIET=1
cd "$REPO"

FAILED=()
run_one() {  # label  cmd...
  local label="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [[ $rc -eq 0 ]]; then
    printf 'PASS  %s\n' "$label"
  else
    printf 'FAIL  %s\n' "$label"
    FAILED+=("$label")
  fi
  [[ $QUIET -eq 0 ]] && printf '%s\n' "$out" | sed 's/^/      /'
  return 0
}

echo "telegram-claude-bot test suite"
echo "=============================="
run_one "room_state   (unit)"        "$PY" tests/test_room_state.py
run_one "approved_users (unit)"      "$PY" tests/test_approved_users.py
run_one "md_telegram  (unit)"        "$PY" tests/test_md_telegram.py
run_one "lib.sh       (helpers)"     bash tests/test_lib.sh
run_one "dispatch     (handlers)"    bash tests/test_dispatch.sh
run_one "listener parse (unit)"      bash tests/test_listener_parse.sh
run_one "wakeup rescue  (unit)"      bash tests/test_wakeup_rescue.sh
run_one "queue coalesce (unit)"      bash tests/test_queue_coalesce.sh
run_one "park rate limit (unit)"     "$PY" tests/test_park_room_rate_limit.py
run_one "429 backoff (unit)"         "$PY" tests/test_streamer_429_backoff.py
run_one "inject reaction (unit)"     "$PY" tests/test_spawn_state_reaction.py
run_one "dashboard snapshot (unit)"  "$PY" tests/test_dashboard_snapshot.py
run_one "pending reacts (unit)"      "$PY" tests/test_pending_reacts.py
run_one "ask_user expiry (unit)"     "$PY" tests/test_ask_user_expiry.py
run_one "nested fence (unit)"        "$PY" tests/test_nested_fence_in_edit_tool.py
run_one "footer buttons (unit)"      "$PY" tests/test_turn_end_footer_buttons.py
run_one "footer survives 429 (unit)" "$PY" tests/test_footer_survives_429.py
run_one "tool marker no-block (unit)" "$PY" tests/test_tool_marker_no_block.py
run_one "streamer no backlog block (unit)" "$PY" tests/test_streamer_no_backlog_block.py
run_one "windows screencapture"      bash tests/test_windows_screencapture.sh
run_one "streamer     (repo smoke)"  "$PY" tests/test_streamer_smoke.py
run_one "group thinking teardown"    "$PY" tests/test_group_thinking_teardown.py
run_one "group silent-drop salvage"  "$PY" tests/test_group_silent_drop.py
run_one "edit no double-send (unit)" "$PY" tests/test_edit_double_send.py
run_one "draft stream (unit)"        "$PY" tests/test_draft_streaming.py
run_one "thinking narration (unit)"  "$PY" tests/test_thinking_narration.py
run_one "compact completion (unit)"  "$PY" tests/test_compact_completion.py
run_one "wakeup tracking (unit)"    "$PY" tests/test_wakeup_tracking.py
run_one "thinking placeholder 429 (unit)" "$PY" tests/test_thinking_placeholder_429.py
run_one "too-long recovery (unit)"   "$PY" tests/test_too_long_recovery.py
run_one "schedule routing (unit)"    "$PY" tests/test_schedule_routing.py
run_one "md_telegram  (repo selftest)" "$PY" scripts/md_telegram.py

echo "------------------------------"
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "ALL SUITES PASSED"
else
  printf '%d SUITE(S) FAILED: %s\n' "${#FAILED[@]}" "${FAILED[*]}"
  exit 1
fi
