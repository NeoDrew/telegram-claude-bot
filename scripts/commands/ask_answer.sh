# commands/ask_answer.sh - deliver an answer to a pending ask_user MCP call.
#
# SOURCED by handle_action.sh. Variables in scope: REPO_ROOT, ROOM_DIR,
# ROOM_CHAT_ID, ARGS, CALLBACK_MSG_ID, and lib.sh helpers.
#
# Triggered by button taps: callback_data = "act:ask_answer __idx__N" or
#                                           "act:ask_answer __skip__"
# Free-text replies are intercepted earlier in listener.sh::dispatch().
#
# Behaviour:
#   1. Reads .ask_user_pending.json BEFORE writing the answer (avoids race
#      where the MCP server deletes the pending file before we read prompt/options).
#   2. Resolves __idx__N to the actual option text via the pending JSON
#      (index routing keeps callback_data under Telegram's 64-byte limit).
#   3. Writes the resolved answer atomically to .ask_user_answer.
#   4. Edits the question message in place to show the answer and remove buttons.
#   5. Outputs nothing to stdout (no new message sent).

PENDING_FILE="$ROOM_DIR/.ask_user_pending.json"
ANSWER_FILE="$ROOM_DIR/.ask_user_answer"

expire_tapped_question() {
  # A tap on a question Claude is no longer waiting for (timed out, or the
  # reply was interrupted). Used to do nothing visible; now it says so and
  # removes the buttons.
  [[ -n "${CALLBACK_MSG_ID:-}" ]] && reply_kb "⌛ This question has expired; Claude carried on without an answer. Send a message if you still want to answer it." "[]" 0
  exit 0
}

[[ -f "$PENDING_FILE" ]] || expire_tapped_question

# Read pending data FIRST so a fast MCP server can't delete it before us.
PENDING_DATA=$(cat "$PENDING_FILE" 2>/dev/null || echo '{}')
# A button from an OLDER question must not answer the one pending now.
PENDING_MSG_ID=$(printf '%s' "$PENDING_DATA" | python3 -c "import json,sys
try: print(json.loads(sys.stdin.read()).get('msg_id') or '')
except Exception: print('')" 2>/dev/null)
if [[ -n "${CALLBACK_MSG_ID:-}" && -n "$PENDING_MSG_ID" && "$PENDING_MSG_ID" != "$CALLBACK_MSG_ID" ]]; then
  expire_tapped_question
fi
QUESTION=$(printf '%s' "$PENDING_DATA" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('prompt', '')[:150])
except Exception:
    print('')
" 2>/dev/null || echo "")

# Resolve index-based callback values to the real option text.
RAW_ARGS="$ARGS"
if [[ "$RAW_ARGS" == __idx__* ]]; then
  IDX="${RAW_ARGS#__idx__}"
  ANSWER=$(printf '%s' "$PENDING_DATA" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    opts = d.get('options', [])
    idx = int(sys.argv[1])
    print(opts[idx] if 0 <= idx < len(opts) else '')
except Exception:
    print('')
" "$IDX" 2>/dev/null || echo "")
  [[ -z "$ANSWER" ]] && ANSWER="[option $IDX]"
else
  ANSWER="$RAW_ARGS"
fi

# Write the answer atomically via a temp file to avoid partial-read races.
TMP_ANSWER="${ANSWER_FILE}.tmp.$$"
printf '%s' "$ANSWER" > "$TMP_ANSWER"
mv -f "$TMP_ANSWER" "$ANSWER_FILE"

# Edit the Telegram question message to show the result and clear the keyboard.
if [[ -n "${CALLBACK_MSG_ID:-}" ]]; then
  if [[ "$ANSWER" == "__skip__" ]]; then
    EDIT_TEXT="<i>Question skipped.</i>"
  else
    SAFE=$(printf '%s' "$ANSWER" | python3 -c \
      "import sys,html; print(html.escape(sys.stdin.read().strip()))" 2>/dev/null \
      || printf '%s' "$ANSWER")
    SAFE_Q=$(printf '%s' "$QUESTION" | python3 -c \
      "import sys,html; print(html.escape(sys.stdin.read().strip()))" 2>/dev/null \
      || printf '%s' "$QUESTION")
    EDIT_TEXT="<b>$SAFE</b>"
    [[ -n "$SAFE_Q" ]] && EDIT_TEXT+="
<i>$SAFE_Q</i>"
  fi
  CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" edit_html \
    "$CALLBACK_MSG_ID" "$EDIT_TEXT" >/dev/null 2>&1 || true
fi
# No stdout output: editing in place, no extra message needed.
