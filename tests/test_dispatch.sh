#!/bin/bash
# test_dispatch.sh - integration-flavoured unit tests for the action layer
# (handle_action.sh dispatcher + scripts/commands/<cmd>.sh handlers).
#
# Fully isolated and network-free: we build a throwaway fake repo in a tmp
# dir, symlink every real script into it EXCEPT tg.sh, and replace tg.sh with
# a recording stub. handle_action.sh computes REPO_ROOT from its own path,
# so it resolves to the tmp repo; every read/write lands in tmp/state, and
# every Telegram call is captured in tmp/tg_calls.log instead of hitting the
# network. The real repo's state/ is never touched.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL="$(cd "$HERE/.." && pwd)"
. "$HERE/lib_assert.sh"

TMP="$(mktemp -d)"
HOME_TESTDIR="$HOME/.cmdbot_test_$$"
trap 'rm -rf "$TMP" "$HOME_TESTDIR"' EXIT

# ---- build the fake repo -------------------------------------------------
mkdir -p "$TMP/scripts" "$TMP/scripts/commands" "$TMP/state/rooms" "$TMP/logs"
for entry in "$REAL/scripts"/*; do
  base="$(basename "$entry")"
  [[ "$base" == "tg.sh" ]] && continue   # tg.sh is stubbed below
  [[ "$base" == "commands" ]] && continue  # symlinked file-by-file below
  ln -s "$entry" "$TMP/scripts/$base"
done
for entry in "$REAL/scripts/commands"/*; do
  ln -s "$entry" "$TMP/scripts/commands/$(basename "$entry")"
done
cat > "$TMP/scripts/tg.sh" <<'STUB'
#!/bin/bash
# Recording stub: never touches the network. Logs the subcommand + args and
# prints a fake message id for the send-style verbs the callers expect.
echo "$*" >> "$(dirname "$0")/../tg_calls.log"
case "${1:-}" in kb|send|photo|editkb|media_group) echo 99999 ;; esac
exit 0
STUB
chmod +x "$TMP/scripts/tg.sh"
# Empty bot token on purpose: the users handler's pending renderer resolves
# @handles via getChatMember only when a token is set, so a blank token keeps
# these handler tests fully offline. Nothing else tested here reads the token
# (tg.sh is stubbed).
printf 'TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=123456\n' > "$TMP/.env"

CHAT="-555000111"
ROOM="$TMP/state/rooms/$CHAT"
RS="$TMP/scripts/room_state.py"

OUT=""; RC=0
# run "<cmd> [args]" -> sets OUT, RC; resets the tg call log.
# The single-string call shape mirrors how the listener invokes
# handle_action.sh from a callback (the whole payload after "act:" is
# passed as one arg).
run() {
  : > "$TMP/tg_calls.log"
  OUT="$(CHAT_ID="$CHAT" bash "$TMP/scripts/handle_action.sh" "$1" 2>/dev/null)"
  RC=$?
}
tg_calls() { cat "$TMP/tg_calls.log" 2>/dev/null; }

echo "== dispatch / handler tests =="

# ---- read-only commands --------------------------------------------------
run "help";         assert_contains "help describes the bot"     "$OUT" "Claude Code"
run "status";       assert_contains "status shows header"        "$OUT" "Status"
run "zzz";          assert_contains "unknown action rejected"    "$OUT" "Unknown action"
# Old typed-only commands /ping and /clear were retired; their verbs must
# fall through to the default branch.
run "ping";         assert_contains "ping removed (now unknown)"  "$OUT" "Unknown action"
run "clear";        assert_contains "clear removed (now unknown)" "$OUT" "Unknown action"

# ---- defensive slash-strip ----------------------------------------------
# Callers that still pass the old "/cmd" wire format should keep working
# during the migration window: handle_action.sh strips a leading slash.
run "/status";      assert_contains "leading-slash status still routes" "$OUT" "Status"

# ---- effort: set / clear / validate / keyboard --------------------------
run "effort high"
assert_contains "effort high acks"        "$OUT" "Effort set to high"
assert_file_eq  "effort high persists"    "$ROOM/effort" "high"

run "effort xhigh"
assert_file_eq  "effort overwrite"        "$ROOM/effort" "xhigh"

run "effort bogus"
assert_contains "effort rejects invalid"  "$OUT" "Invalid effort"
assert_file_eq  "invalid leaves file"     "$ROOM/effort" "xhigh"

run "effort default"
assert_contains "effort default acks"     "$OUT" "Effort cleared"
assert_no_file  "effort default clears"   "$ROOM/effort"

run "effort"
assert_contains "bare effort draws kb"    "$(tg_calls)" "kb"
assert_eq       "bare effort no stdout"   "$OUT" ""

# ---- model --------------------------------------------------------------
# "opus" tracks DEFAULT_MODEL (currently opus 5.5); "opus48" is the
# fixed synonym for the former default.
run "model opus"
assert_file_eq  "model resolves alias (bare opus tracks default)" "$ROOM/model" "claude-opus-5-5"
run "model opus48"
assert_file_eq  "model resolves alias (opus48 fixed)" "$ROOM/model" "claude-opus-4-8"
# sonnet5/sonnet46 (not the old bare "sonnet") are what the real buttons
# send; see model_effort.py's OPTIONS, the single source of truth
# resolve_model_alias() now looks up directly. Regression coverage for a
# bug where OPTIONS split "sonnet" into sonnet5/sonnet46 but
# model.sh's own hand-maintained alias table was never updated to match,
# so both sonnet buttons silently fell through to "Unknown model".
run "model sonnet5"
assert_file_eq  "model overwrite (sonnet 5)"   "$ROOM/model" "claude-sonnet-5"
run "model sonnet46"
assert_file_eq  "model overwrite (sonnet 4.6)" "$ROOM/model" "claude-sonnet-4-6"
run "model opus5"
assert_file_eq  "model overwrite (opus 5)"     "$ROOM/model" "claude-opus-5"
run "model opus55"
assert_file_eq  "model overwrite (opus 5.5)"   "$ROOM/model" "claude-opus-5-5"

# A model CHANGE resets the room's effort to default (levels are model-
# specific; a stale ultracode must not survive a switch to e.g. haiku).
echo "ultracode" > "$ROOM/effort"
run "model haiku"
assert_file_eq  "model change persists"      "$ROOM/model" "claude-haiku-4-5"
assert_no_file  "model change resets effort" "$ROOM/effort"
assert_contains "model change notes reset"   "$OUT" "Effort reset to default"

# Re-selecting the CURRENT model is a no-op and keeps the effort.
echo "high" > "$ROOM/effort"
run "model haiku"
assert_file_eq  "same model keeps effort"    "$ROOM/effort" "high"
assert_contains "same model still acks"      "$OUT" "Model set to claude-haiku-4-5"

# Clearing an override is also a model change: effort resets too.
run "model default"
assert_no_file  "model default clears model"  "$ROOM/model"
assert_no_file  "model default resets effort" "$ROOM/effort"

# ---- tune (combined model + effort screen) -------------------------------
run "tune"
assert_contains "bare tune draws kb"          "$(tg_calls)" "kb"
assert_contains "tune kb has model section"   "$(tg_calls)" "tune model"
assert_contains "tune kb has effort section"  "$(tg_calls)" "tune effort"

# Tapping a model on the combined screen goes through the real model
# handler, so the change-resets-effort semantics hold here too.
echo "ultracode" > "$ROOM/effort"
run "tune model fable"
assert_file_eq  "tune model sets model"       "$ROOM/model" "claude-fable-5-1"
assert_no_file  "tune model change resets effort" "$ROOM/effort"
assert_contains "tune re-renders the screen"  "$(tg_calls)" "kb"

run "tune effort high"
assert_file_eq  "tune effort sets effort"     "$ROOM/effort" "high"

# Re-selecting the current model keeps the effort (no-op semantics).
run "tune model fable"
assert_file_eq  "tune same model keeps effort" "$ROOM/effort" "high"

# Fable is ultracode-eligible, so the combined screen must offer it.
run "tune"
assert_contains "tune offers ultracode on fable" "$(tg_calls)" "ultracode"

run "model default"
run "effort default"

# ---- more (submenu) -------------------------------------------------------
run "more"
assert_contains "more draws kb"               "$(tg_calls)" "kb"
assert_contains "more lists rooms"            "$(tg_calls)" "act:rooms"
assert_contains "more lists alerts"           "$(tg_calls)" "act:alerts"
assert_contains "more lists mentions (group)" "$(tg_calls)" "act:mentions"

# ---- alerts channel ------------------------------------------------------
# Bind/unbind the GLOBAL alerts channel (state/.alerts_chat_id). The test
# room is a group (negative id), so binding is allowed.
run "alerts here"
assert_file_eq  "alerts here binds this room" "$TMP/state/.alerts_chat_id" "$CHAT"
assert_contains "alerts here acks"            "$OUT" "alerts channel"

run "alerts"
assert_contains "bare alerts draws kb"        "$(tg_calls)" "kb"

run "alerts off"
assert_no_file  "alerts off unbinds"          "$TMP/state/.alerts_chat_id"
assert_contains "alerts off acks fallback"    "$OUT" "DM"

# ---- label --------------------------------------------------------------
run "label My Room"
assert_file_eq  "label persists name"     "$ROOM/label" "My Room"
assert_contains "label acks rename"       "$OUT" "My Room"

# ---- cancel ------------------------------------------------------------
# cancel handler goes through reply_kb, so assert against tg_calls.log.
run "cancel"
assert_contains "cancel idle says nothing to cancel" "$(tg_calls)" "Nothing running right now"

# A live spawn with an inbox: cancel interrupts the turn (a stream-json
# control request), it does not kill anything or clear the queue.
mkdir -p "$ROOM"
sleep 60 & FAKE_SPAWN=$!
echo "$FAKE_SPAWN" > "$ROOM/busy.lock"
touch "$ROOM/.inbox_ready"
printf '%s\n' '{"type":"user","message":{"role":"user","content":"the cut-short job"}}' \
  '{"type":"user","message":{"role":"user","content":"queued msg"}}' > "$ROOM/inbox.ndjson"
printf '[41]\n[42]\n' > "$ROOM/.pending_reacts"
echo 'kept in queue' > "$ROOM/queue.jsonl"
run "cancel"
assert_contains "cancel live spawn interrupts" "$(tg_calls)" "Interrupting"
assert_contains "interrupt is a control request" "$(tail -1 "$ROOM/inbox.ndjson")" '"subtype":"interrupt"'
assert_eq "interrupt keeps reacts parallel" "$(tail -1 "$ROOM/.pending_reacts")" "[]"
assert_eq "interrupt leaves the spawn running" "$(kill -0 "$FAKE_SPAWN" 2>/dev/null && echo alive)" "alive"
assert_file_eq "interrupt keeps the queue" "$ROOM/queue.jsonl" "kept in queue"

# The interrupt went unanswered: the next tap force-stops, and the line
# Claude never read moves to the queue instead of being thrown away.
touch -t 202001010000 "$ROOM/.interrupt_requested"
run "cancel"
assert_contains "stuck interrupt force-stops" "$(tg_calls)" "force stopped"
sleep 0.2
assert_eq "force stop kills the spawn" "$(kill -0 "$FAKE_SPAWN" 2>/dev/null && echo alive || echo gone)" "gone"
assert_eq "force stop requeues the unread message, ids kept" "$(tail -1 "$ROOM/queue.jsonl")" "queued msg"$'\x1f'"42"
assert_eq "force stop keeps what was queued" "$(head -1 "$ROOM/queue.jsonl")" "kept in queue"
rm -f "$ROOM/queue.jsonl" "$ROOM/.inbox_ready"

# ---- close (menu collapse, not delete) -----------------------------------
# Used to tg.sh delete the message; now collapses it to a single "☰ Menu"
# button in place, same affordance as the turn-end footer's Menu shortcut.
# Goes through reply_kb, assert against tg_calls.log. The text above the
# button went through two earlier versions: a "🎯 <room label>" heading,
# then a bare middle dot (Telegram rejects truly empty/invisible text).
# Both were dropped in favour of the word "Menu", matching the button below
# it. Assert both discarded options are genuinely gone, not just that the
# current one works.
run "close"
assert_contains     "close collapses to a Menu button, doesn't delete" "$(tg_calls)" "☰ Menu"
assert_contains     "collapsed button re-expands via act:menu"         "$(tg_calls)" "act:menu"
assert_contains     "collapsed text reads 'Menu', matching the button" "$(tg_calls)" "kb Menu "
assert_not_contains "collapse drops the room-label heading text"       "$(tg_calls)" "🎯"
assert_not_contains "collapse drops the earlier ugly-dot placeholder"  "$(tg_calls)" "kb · "

# ---- menu_new / compact_new: footer buttons must NOT edit in place ------
# Regression test: tapping the footer's Menu button used to overwrite the
# "Worked for" footer message. The plain menu/compact verbs correctly
# edit the tapped message when CALLBACK_MSG_ID is set, that's the whole
# point of every other menu button. The footer-only *_new variants exist
# specifically to send a FRESH message instead, even with CALLBACK_MSG_ID
# set, so the turn-end footer's own "Worked for..." text survives a tap.
runCB() {  # runCB "<cmd>" <callback_msg_id> -> sets OUT, resets tg_calls.log
  : > "$TMP/tg_calls.log"
  OUT="$(CHAT_ID="$CHAT" CALLBACK_MSG_ID="$2" bash "$TMP/scripts/handle_action.sh" "$1" 2>/dev/null)"
}
runCB "menu" "99001"
assert_contains "plain act:menu edits the tapped message in place" "$(tg_calls)" "editkb 99001"
assert_contains "plain act:menu's own close row still collapses"   "$(tg_calls)" "act:close\""

runCB "menu_new" "99002"
assert_not_contains "act:menu_new does NOT edit the tapped message" "$(tg_calls)" "editkb"
assert_contains     "act:menu_new sends a fresh message instead"    "$(tg_calls)" "kb "

runCB "compact_new" "99003"
assert_not_contains "act:compact_new does NOT edit the tapped message" "$(tg_calls)" "editkb"
assert_contains     "act:compact_new sends a fresh message instead"    "$(tg_calls)" "kb "

# ---- menu_new's own close row must DELETE, not collapse to a stub -------
# Regression test for duplicate "Menu" stubs piling up: a footer-spawned menu (act:menu_new) sits
# right under a footer that already has its own permanent "☰ Menu"
# button, so collapsing this one too (the plain menu.sh default) left a
# second, redundant "Menu" stub next to it, one more copy per tap.
runCB "menu_new" "99004"
# (Not matching the "✕ close" label text directly: python's json.dumps
# ascii-escapes it on the wire, Telegram decodes it identically either
# way, but that makes a literal-character substring match unreliable.
# The callback_data checks below are the load-bearing assertions.)
assert_contains     "footer-spawned menu's close row wires to close_delete"    "$(tg_calls)" "act:close_delete"
assert_not_contains "footer-spawned menu's close row is NOT the collapsing one" "$(tg_calls)" "▾ collapse"

runCB "close_delete" "99005"
assert_contains     "act:close_delete deletes the tapped message"       "$(tg_calls)" "delete 99005"
assert_not_contains "act:close_delete does not collapse (no editkb/kb)" "$(tg_calls)" "editkb"

# ---- ask_user answers -----------------------------------------------------
# A tap on a question nobody is waiting for says it expired (it used to do
# nothing visible), and a tap on an OLDER question never answers the newer
# one that is pending.
rm -f "$ROOM/.ask_user_pending.json" "$ROOM/.ask_user_answer"
runCB "ask_answer __idx__0" 501
assert_contains "tap with nothing pending says expired" "$(tg_calls)" "expired"
printf '%s' '{"prompt":"Ship it?","options":["Yes","No"],"msg_id":600}' > "$ROOM/.ask_user_pending.json"
runCB "ask_answer __idx__0" 501
assert_contains "tap on an older question says expired" "$(tg_calls)" "expired"
assert_no_file  "older tap does not answer the pending question" "$ROOM/.ask_user_answer"
runCB "ask_answer __idx__1" 600
assert_file_eq  "tap on the pending question answers it" "$ROOM/.ask_user_answer" "No"
assert_contains "answered question keeps the bold question" "$(tg_calls)" "<b>Ship it?</b>"
assert_contains "answered question adds Selected: answer"   "$(tg_calls)" "Selected: No"
rm -f "$ROOM/.ask_user_pending.json" "$ROOM/.ask_user_answer"

# ---- new lifecycle ------------------------------------------------------
run "new"
assert_contains "new no-session message"  "$OUT" "Nothing to reset"

python3 "$RS" set "$ROOM/sessions.json" --cwd /Users/x/projects/q --session SID-LIVE
run "new"
assert_contains "new resets session"      "$(tg_calls)" "New session started"
assert_contains "new offers an undo"      "$(tg_calls)" "act:new undo"
assert_eq       "new drops session"       "$(python3 "$RS" get-session "$ROOM/sessions.json")" ""
run "new undo"
assert_contains "undo restores the conversation" "$(tg_calls)" "Previous conversation restored"
assert_eq       "undo puts the old session back" "$(python3 "$RS" get-session "$ROOM/sessions.json")" "SID-LIVE"
run "new undo"
assert_contains "a second undo has nothing to do" "$(tg_calls)" "Nothing to undo"
run "new"
python3 "$RS" set "$ROOM/sessions.json" --cwd /Users/x/projects/elsewhere
run "new undo"
assert_contains "undo refuses after a folder switch" "$(tg_calls)" "switched folders"
python3 "$RS" set "$ROOM/sessions.json" --cwd /Users/x/projects/q
rm -f "$ROOM/.prev_session"

# ---- cd resolution + session-drop semantics -----------------------------
mkdir -p "$HOME_TESTDIR"
EXPECT_CWD="$(python3 -c "import os;print(os.path.realpath('$HOME_TESTDIR'))")"
python3 "$RS" set "$ROOM/sessions.json" --cwd /Users/x/projects/old --session OLDSID
run "cd $HOME_TESTDIR"
assert_eq       "cd updates cwd"          "$(python3 "$RS" get-cwd "$ROOM/sessions.json")" "$EXPECT_CWD"
assert_eq       "cd to new dir drops session" "$(python3 "$RS" get-session "$ROOM/sessions.json")" ""
assert_contains "cd reports new cwd"      "$OUT" "$EXPECT_CWD"

run "cd $HOME_TESTDIR"
assert_contains "cd same dir is a no-op"  "$OUT" "Already in"

run "cd /etc"
assert_contains "cd outside HOME refused" "$OUT" "Refused"
assert_eq       "refused cd keeps cwd"    "$(python3 "$RS" get-cwd "$ROOM/sessions.json")" "$EXPECT_CWD"

# Switching folders while a reply is running asks first; the confirm
# button ("--now") switches and stops it. spawn_state is stubbed to report
# a reply in progress (the fake spawn has no transcript to classify).
mkdir -p "$HOME_TESTDIR/other"
python3 "$RS" set "$ROOM/sessions.json" --cwd "$EXPECT_CWD" --session MIDSID
mv "$TMP/scripts/spawn_state.py" "$TMP/scripts/spawn_state.py.real"
printf '%s\n' 'import json; print(json.dumps({"state": "tool"}))' > "$TMP/scripts/spawn_state.py"
sleep 100 & SPAWN_PID=$!
echo "$SPAWN_PID" > "$ROOM/busy.lock"
run "cd $HOME_TESTDIR/other"
assert_contains "cd mid-reply asks first"          "$(tg_calls)" "A reply is still running"
assert_contains "cd confirm button switches now"   "$(tg_calls)" "act:cd --now $HOME_TESTDIR/other"
assert_eq       "cd mid-reply keeps the session"   "$(python3 "$RS" get-session "$ROOM/sessions.json")" "MIDSID"
assert_eq       "cd mid-reply leaves the reply running" "$(kill -0 "$SPAWN_PID" 2>/dev/null && echo alive)" "alive"
run "cd --now $HOME_TESTDIR/other"
assert_contains "cd --now switches"                "$OUT" "other"
assert_eq       "cd --now drops the session"       "$(python3 "$RS" get-session "$ROOM/sessions.json")" ""
kill -9 "$SPAWN_PID" 2>/dev/null; rm -f "$ROOM/busy.lock"
mv "$TMP/scripts/spawn_state.py.real" "$TMP/scripts/spawn_state.py"

# ---- restart: neutralised execute ----------------------
# BOT_RESTART_CMD is forced to a no-op (`true`) and BOT_RESTART_DELAY=0 so the
# suite can NEVER kickstart the real service. We assert routing + the ack copy.
runR() { : > "$TMP/tg_calls.log"; OUT="$(CHAT_ID="$CHAT" BOT_RESTART_CMD=true BOT_RESTART_DELAY=0 bash "$TMP/scripts/handle_action.sh" "$1" 2>/dev/null)"; }
runR "restart"
assert_contains "restart acks"                 "$(tg_calls)" "Restarting"
assert_file_exists "restart drops announce marker" "$TMP/state/.restart_announce"
assert_contains    "announce marker names this room" "$(cat "$TMP/state/.restart_announce" 2>/dev/null)" "$CHAT"

# ---- users: two-section Members/Pending + one-tap moves -----------------
# Token is blank in this harness, so the pending renderer's getChatMember
# resolve is skipped, so it stays fully offline. approved_users.py (symlinked) writes to
# $TMP/state, so the real allowlist is never touched.
AU="$TMP/scripts/approved_users.py"
run "users"
assert_contains "users shows Members section"   "$(tg_calls)" "Members"
assert_contains "users shows Pending section"   "$(tg_calls)" "Pending"
assert_contains "users offers add-by-contact"   "$(tg_calls)" "act:users add"

run "users approve 111222333 test_handle"
assert_eq       "approve writes a member"        "$(python3 "$AU" flatten)" "$CHAT:111222333"
run "users"
assert_contains "member row has a remove button" "$(tg_calls)" "act:users rm 111222333"

run "users rm 111222333"
assert_eq       "rm clears the member"           "$(python3 "$AU" flatten)" ""
assert_eq       "rm demotes to pending"          "$(python3 "$AU" pending-list "$CHAT" | cut -f1)" "111222333"

run "users approve 123456"
assert_contains "approving the owner is a no-op" "$(tg_calls)" "owner is always allowed"
assert_eq       "owner not stored as a member"   "$(python3 "$AU" flatten)" ""

run "users approve notanumber"
assert_contains "approve rejects a bad id"       "$(tg_calls)" "Couldn't read"

run "users add"
assert_contains "users add pops the picker"      "$(tg_calls)" "request_user_kb"

# ---- autocompact: per-room threshold picker (40 to 90) -------------------
run "autocompact"
assert_contains "bare autocompact draws kb"      "$(tg_calls)" "act:autocompact 40"
assert_contains "autocompact kb tops out at 90"  "$(tg_calls)" "act:autocompact 90"
run "autocompact 65"
assert_file_eq  "autocompact 65 persists"        "$ROOM/autocompact" "65"
run "autocompact 95"
assert_contains "autocompact rejects >90"        "$OUT" "Invalid auto compact"
assert_file_eq  "invalid leaves file"            "$ROOM/autocompact" "65"
run "autocompact 30"
assert_contains "autocompact rejects <40"        "$OUT" "Invalid auto compact"
run "more"
assert_contains "more shows autocompact value"   "$(tg_calls)" "auto compact: 65%"
run "autocompact default"
assert_no_file  "autocompact default clears"     "$ROOM/autocompact"

# ---- mentions: per-room @-mentions-only toggle --------------------------
run "mentions"
assert_contains "mentions toggles ON"            "$OUT" "ON"
assert_file_eq  "mentions writes 'on'"           "$ROOM/mentions_only" "on"
run "mentions"
assert_contains "mentions toggles back off"      "$OUT" "off"
assert_no_file  "mentions clears the file"       "$ROOM/mentions_only"

# ---- plugin update: kills a busy room's spawn instead of hoping a future
# message eventually restarts it -------------------------------------------
# Regression for a bug: the "next message restarts the process" promise was
# false for the exact rooms most likely to test a fresh plugin update: a
# long-lived, currently-busy persistent spawn just keeps
# running (and re-using its already-spawned MCP server children, still
# reading the pre-update cache) since a persistent room's process does NOT
# replace itself just because a message arrives. Fix: plugin.sh now kills
# the room's spawn itself (the "stale" path listener.sh's own automatic
# stale-code refresh uses) the moment an update succeeds.
#
# CLAUDE_BIN (checked by plugin.sh, NOT CLAUDE_BIN_OVERRIDE which is
# spawn_claude.sh's own separate knob) points at a tiny fake here that only
# understands the `plugin list`/`plugin update` subcommands this test needs.
FAKE_PLUGIN_CLAUDE="$TMP/fake_claude_plugin.sh"
cat > "$FAKE_PLUGIN_CLAUDE" <<'PLUGINSTUB'
#!/bin/bash
if [[ "$1" == "plugin" && "$2" == "list" ]]; then
  echo "Installed plugins:"
  echo "❯ exampleplugin@example-marketplace"
  exit 0
fi
if [[ "$1" == "plugin" && "$2" == "update" ]]; then
  echo "Updated exampleplugin to version 9.9.9"
  exit 0
fi
exit 1
PLUGINSTUB
chmod +x "$FAKE_PLUGIN_CLAUDE"
export CLAUDE_BIN="$FAKE_PLUGIN_CLAUDE"

# A running spawn is NOT stopped (that cut replies off mid-sentence); the
# update is marked in state/.plugins_updated, which listener.sh's stale-spawn
# check applies at each room's next message between turns.
sleep 100 &
SPAWN_PID=$!
echo "$SPAWN_PID" > "$ROOM/busy.lock"
rm -f "$TMP/state/.plugins_updated"
run "plugin update-all"
assert_contains "plugin update says it applies from the next message" "$(tg_calls)" "from its next message"
assert_eq "plugin update leaves the running spawn alone" "$(kill -0 "$SPAWN_PID" 2>/dev/null && echo alive)" "alive"
assert_file_exists "plugin update marks every room for refresh" "$TMP/state/.plugins_updated"
kill -9 "$SPAWN_PID" 2>/dev/null; rm -f "$ROOM/busy.lock"

# Changing the model mid-reply must not stop the reply either; the
# stale-spawn check applies it at the next message between turns.
sleep 100 &
SPAWN_PID=$!
echo "$SPAWN_PID" > "$ROOM/busy.lock"
run "model haiku"
assert_contains "busy model change applies from the next message" "$OUT$(tg_calls)" "takes over from your next message"
assert_eq "busy model change leaves the running spawn alone" "$(kill -0 "$SPAWN_PID" 2>/dev/null && echo alive)" "alive"
kill -9 "$SPAWN_PID" 2>/dev/null; rm -f "$ROOM/busy.lock" "$ROOM/model" "$ROOM/effort"

unset CLAUDE_BIN

# ---- routing completeness: every case pattern has a handler file ---------
# Parses handle_action.sh's case statement keys (e.g. "effort)", "menu|start)")
# and asserts each verb has a matching scripts/commands/<verb>.sh file. Sticks
# to POSIX-portable grep/sed (BSD awk's match() doesn't take a captures arg).
while read -r pat; do
  first="${pat%%|*}"          # "menu|start" -> "menu"
  [[ -z "$first" || "$first" == "*" ]] && continue
  assert_file_exists "route $pat -> commands/$first.sh" "$REAL/scripts/commands/$first.sh"
done < <(grep -oE '^[[:space:]]+[a-z|]+\)' "$REAL/scripts/handle_action.sh" \
         | sed -E 's/^[[:space:]]+//; s/\)$//')

# ---- isolation: the REAL repo state was never touched --------------------
assert_no_file "real repo state untouched" "$REAL/state/rooms/$CHAT"

finish
