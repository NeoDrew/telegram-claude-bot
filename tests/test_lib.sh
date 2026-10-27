#!/bin/bash
# test_lib.sh - unit tests for scripts/lib.sh shared helpers:
#   extract_var  (the .env reader that must NOT bulk-source / leak keys)
#   reply_kb     (keyboard JSON + auto back-to-menu row)
#   kill_tree    (recursive descendant kill; grandchildren must die too)

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL="$(cd "$HERE/.." && pwd)"
. "$HERE/lib_assert.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
. "$REAL/scripts/lib.sh"

echo "== lib.sh helper tests =="

# ---- extract_var ---------------------------------------------------------
REPO_ROOT="$TMP"
cat > "$TMP/.env" <<'ENV'
TELEGRAM_BOT_TOKEN=abc123
TELEGRAM_CHAT_ID=999
WEIRD=a=b=c
EMPTY=
ANTHROPIC_API_KEY=should-stay-put
ENV
assert_eq "extract_var basic value"        "$(extract_var TELEGRAM_CHAT_ID)" "999"
assert_eq "extract_var token"              "$(extract_var TELEGRAM_BOT_TOKEN)" "abc123"
assert_eq "extract_var keeps = in value"   "$(extract_var WEIRD)" "a=b=c"
assert_eq "extract_var empty value"        "$(extract_var EMPTY)" ""
assert_eq "extract_var missing key empty"  "$(extract_var NOPE)" ""
# Prefix safety: TELEGRAM_CHAT must NOT match TELEGRAM_CHAT_ID (anchored ^KEY=).
assert_eq "extract_var no prefix bleed"    "$(extract_var TELEGRAM_CHAT)" ""
# extract_var reads a single var; it must not export the whole file. Confirm
# the proxy key is NOT now in this shell's environment.
assert_eq "extract_var does not leak env"  "${ANTHROPIC_API_KEY:-unset}" "unset"

# ---- reply_kb (with a stubbed tg.sh) -------------------------------------
RR="$TMP/rr"
mkdir -p "$RR/scripts"
cat > "$RR/scripts/tg.sh" <<'STUB'
#!/bin/bash
echo "$*" >> "$(dirname "$0")/../tg_calls.log"
echo 1
STUB
chmod +x "$RR/scripts/tg.sh"

REPO_ROOT="$RR"
ROOM_CHAT_ID="1"
unset CALLBACK_MSG_ID 2>/dev/null || true

: > "$RR/tg_calls.log"
reply_kb "pick one" '[[{"text":"A","callback_data":"/a"}]]'
CALLS="$(cat "$RR/tg_calls.log")"
assert_contains "reply_kb routes to kb send"   "$CALLS" "kb"
assert_contains "reply_kb keeps original btn"  "$CALLS" "callback_data"
assert_contains "reply_kb appends back row"    "$CALLS" "Menu"

# with_back=0 (main menu only): no extra back row appended.
: > "$RR/tg_calls.log"
reply_kb "main" '[[{"text":"only","callback_data":"/x"}]]' 0
CALLS="$(cat "$RR/tg_calls.log")"
assert_not_contains "reply_kb with_back=0 no back row" "$CALLS" "Menu"

# Malformed keyboard JSON must not crash reply_kb (it falls through).
: > "$RR/tg_calls.log"
reply_kb "bad" 'NOT JSON' 2>/dev/null
assert_contains "reply_kb survives bad kb json" "$(cat "$RR/tg_calls.log")" "kb"

# ---- announce_restart_if_marked -----------------------------------------
# Reuses the stub tg.sh repo ($RR) from the reply_kb tests above.
REPO_ROOT="$RR"
mkdir -p "$RR/state"

# Fresh marker → sends "Back up" to the named chat and consumes the marker.
: > "$RR/tg_calls.log"
printf '%s\n' "1111111111" > "$RR/state/.restart_announce"
announce_restart_if_marked
assert_contains "announce sends back-up msg" "$(cat "$RR/tg_calls.log")" "Back up"
assert_no_file  "announce consumes marker"   "$RR/state/.restart_announce"

# Stale marker (mtime far in the past) → consumed but NO send (a failed
# kickstart must not fire a spurious "back up" on a later unrelated boot).
: > "$RR/tg_calls.log"
printf '%s\n' "1111111111" > "$RR/state/.restart_announce"
touch -t 200001010000 "$RR/state/.restart_announce"
announce_restart_if_marked
assert_eq      "stale marker: no send"   "$(cat "$RR/tg_calls.log")" ""
assert_no_file "stale marker consumed"   "$RR/state/.restart_announce"

# Missing marker → no-op, no crash, no send.
: > "$RR/tg_calls.log"
announce_restart_if_marked
assert_eq "missing marker: no send" "$(cat "$RR/tg_calls.log")" ""

# Invalid chat id in marker → consumed, but no send (never message "chat 0").
: > "$RR/tg_calls.log"
printf '%s\n' "0" > "$RR/state/.restart_announce"
announce_restart_if_marked
assert_eq      "announce: id 0 no send"   "$(cat "$RR/tg_calls.log")" ""
assert_no_file "announce: id 0 consumed"  "$RR/state/.restart_announce"

: > "$RR/tg_calls.log"
printf '%s\n' "not-a-number" > "$RR/state/.restart_announce"
announce_restart_if_marked
assert_eq      "announce: non-numeric no send"  "$(cat "$RR/tg_calls.log")" ""
assert_no_file "announce: non-numeric consumed" "$RR/state/.restart_announce"

# Empty marker → consumed, no send.
: > "$RR/tg_calls.log"
: > "$RR/state/.restart_announce"
announce_restart_if_marked
assert_eq      "announce: empty marker no send"  "$(cat "$RR/tg_calls.log")" ""
assert_no_file "announce: empty marker consumed" "$RR/state/.restart_announce"

# Valid id wrapped in whitespace/newlines → trimmed, sends.
: > "$RR/tg_calls.log"
printf '  1111111111  \n' > "$RR/state/.restart_announce"
announce_restart_if_marked
assert_contains "announce: whitespace id trimmed + sent" "$(cat "$RR/tg_calls.log")" "Back up"
assert_no_file  "announce: whitespace marker consumed"   "$RR/state/.restart_announce"

# ---- kill_tree kills grandchildren --------------------------------------
# `bash -c 'sleep 30 & wait'` keeps bash alive as the PARENT of the sleep
# (a plain `bash -c 'sleep 30'` would exec sleep directly, leaving no
# grandchild). pkill -P would miss the sleep; kill_tree must not.
bash -c 'sleep 30 & wait' &
GP=$!
sleep 0.3
CHILD="$(pgrep -P "$GP" 2>/dev/null | head -1)"
kill_tree "$GP" TERM
sleep 0.4
assert_eq "kill_tree kills the root"        "$(kill -0 "$GP" 2>/dev/null && echo alive || echo dead)" "dead"
if [[ -n "$CHILD" ]]; then
  assert_eq "kill_tree kills the grandchild" "$(kill -0 "$CHILD" 2>/dev/null && echo alive || echo dead)" "dead"
else
  _fail "kill_tree grandchild captured" "no child pid found to verify"
fi

# ---- is_valid_chat_id ---------------------------------------------------
# Accept: real Telegram-shaped ids (positive user_ids, negative group ids,
# negative supergroup ids in the -100xxx form). Reject: empty, non-numeric,
# zero, leading zeros, decimals, and obvious test placeholders
# (anything with magnitude < 10^6, like a 123456 placeholder).
assert_eq "is_valid_chat_id real DM user_id"        "$(is_valid_chat_id 1111111111 && echo yes || echo no)" "yes"
assert_eq "is_valid_chat_id real supergroup id"     "$(is_valid_chat_id -1001000000002 && echo yes || echo no)" "yes"
assert_eq "is_valid_chat_id real plain group id"    "$(is_valid_chat_id -1234567 && echo yes || echo no)" "yes"
assert_eq "is_valid_chat_id positive at threshold"  "$(is_valid_chat_id 1000000 && echo yes || echo no)" "yes"

assert_eq "is_valid_chat_id rejects 123456 ghost"   "$(is_valid_chat_id 123456 && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects 999"            "$(is_valid_chat_id 999 && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects zero"           "$(is_valid_chat_id 0 && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects empty"          "$(is_valid_chat_id "" && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects non-numeric"    "$(is_valid_chat_id abc && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects mixed"          "$(is_valid_chat_id 123abc && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects decimal"        "$(is_valid_chat_id 1234567.0 && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects leading zero"   "$(is_valid_chat_id 01234567 && echo yes || echo no)" "no"
assert_eq "is_valid_chat_id rejects whitespace"     "$(is_valid_chat_id "  1111111111 " && echo yes || echo no)" "no"

# ---- mentions_bot (the group @-mention gate) ----------------------------
B="your_claude_bot"
mb() { mentions_bot "$1" "$B" && echo y || echo n; }
assert_eq "mentions_bot: plain mention"          "$(mb 'hey @your_claude_bot do x')" "y"
assert_eq "mentions_bot: at start of message"    "$(mb '@your_claude_bot hello')" "y"
assert_eq "mentions_bot: at end of message"      "$(mb 'ping @your_claude_bot')" "y"
assert_eq "mentions_bot: case-insensitive"       "$(mb '@your_claude_bot hi')" "y"
assert_eq "mentions_bot: trailing punctuation"   "$(mb '@your_claude_bot, please')" "y"
assert_eq "mentions_bot: no mention at all"      "$(mb 'just chatting amongst ourselves')" "n"
assert_eq "mentions_bot: a different handle"     "$(mb 'hi @someone_else')" "n"
assert_eq "mentions_bot: longer superset handle" "$(mb '@your_claude_bot_two yo')" "n"
assert_eq "mentions_bot: bare word, no @"        "$(mb 'your_claude_bot without the at')" "n"

# ---- should_alert_rate_limit (global, not per-room, alerts dedup) -------
# Regression test for duplicate rate-limit alerts (e.g. from subagents
# working in several rooms at once): all rooms share one
# Anthropic account, so several rooms hitting the SAME shared limit at
# once must alert once, globally, not once per room.
mkdir -p "$TMP/state"
rm -f "$TMP/state/.rl_alerted"
sar() { should_alert_rate_limit "$1" "$2" && echo alerted || echo deduped; }
assert_eq "should_alert_rate_limit: first hit alerts" \
  "$(sar 1790000000 '5-hour session limit')" "alerted"
assert_eq "should_alert_rate_limit: a SECOND room hitting the identical (resume_at, type) is deduped" \
  "$(sar 1790000000 '5-hour session limit')" "deduped"
assert_eq "should_alert_rate_limit: a DIFFERENT limit type at the same resume_at still alerts" \
  "$(sar 1790000000 'weekly limit')" "alerted"
assert_eq "should_alert_rate_limit: a DIFFERENT resume_at for the same type still alerts" \
  "$(sar 1799999999 '5-hour session limit')" "alerted"

# ---- bot_config: environment, then .env, then default ----------------------
CFG=$(mktemp -d)
REPO_ROOT="$CFG"
printf 'BOT_PROJECTS_DIR=~/work\nBOT_LAUNCHD_LABEL=com.from.env\n' > "$CFG/.env"
assert_eq "bot_config: default when unset"   "$(bot_config BOT_PRIMARY_HOST fallback)" "fallback"
assert_eq "bot_config: .env value"            "$(bot_label)" "com.from.env"
assert_eq "bot_config: environment wins"      "$(BOT_LAUNCHD_LABEL=com.from.shell bot_label)" "com.from.shell"
assert_eq "projects_dir: ~ expanded"          "$(projects_dir)" "$HOME/work"
assert_eq "notify_cmd: defaults to repo copy" "$(notify_cmd)" "$CFG/scripts/notify-cmd"
rm "$CFG/.env"
assert_eq "bot_label: built-in default"       "$(bot_label)" "com.telegram-claude-bot"
assert_eq "projects_dir: built-in default"    "$(projects_dir)" "$HOME"
PYCFG=$(REPO_ROOT_PY="$REAL" python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT_PY'], 'scripts'))
import bot_config
bot_config.REPO_ROOT = __import__('pathlib').Path('$CFG')
os.environ.pop('BOT_LAUNCHD_LABEL', None)
print(bot_config.get('BOT_LAUNCHD_LABEL', 'com.telegram-claude-bot'), bot_config.projects_dir() == os.path.expanduser('~'))
")
assert_eq "bot_config.py: same defaults as bash" "$PYCFG" "com.telegram-claude-bot True"
rm -rf "$CFG"

finish
