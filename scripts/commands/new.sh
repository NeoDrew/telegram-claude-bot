# commands/new.sh - handler for /new
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    RS="$REPO_ROOT/scripts/room_state.py"
    CWD=$(python3 "$RS" get-cwd "$SESSIONS_FILE")
    PREV_FILE="$ROOM_DIR/.prev_session"   # "<cwd>\t<session_id>" for undo
    if [[ "${ARGS:-}" == "undo" ]]; then
      # One tap used to wipe the conversation with no way back (the button
      # sits next to 📁 cwd). Undo restores the session it cleared, as
      # long as the folder is still the same one.
      PREV_CWD=""; PREV_SID=""
      [[ -f "$PREV_FILE" ]] && IFS=$'\t' read -r PREV_CWD PREV_SID < "$PREV_FILE"
      if [[ -z "$PREV_SID" ]]; then
        reply_kb "Nothing to undo." "[]"
      elif [[ "$PREV_CWD" != "$CWD" ]]; then
        reply_kb "Can't undo: this chat has switched folders since." "[]"
      else
        # A fresh session may already be running; stop it quietly (no
        # "cancelled" badge) so the next message resumes the old one.
        is_busy "$ROOM_CHAT_ID" && kill_room_spawn "$ROOM_DIR" stale
        python3 "$RS" set "$SESSIONS_FILE" --session "$PREV_SID"
        rm -f "$PREV_FILE"
        reply_kb "↩ Previous conversation restored. Your next message continues it." "[]"
      fi
    elif SID=$(python3 "$RS" get-session "$SESSIONS_FILE") && [[ -n "$SID" ]]; then
      printf '%s\t%s\n' "$CWD" "$SID" > "$PREV_FILE"
      python3 "$RS" clear-session "$SESSIONS_FILE"
      kill_room_spawn "$ROOM_DIR" 1
      reply_kb "New session started in $CWD. Next message begins fresh context." \
        '[[{"text":"↩ Undo","callback_data":"act:new undo"}]]'
    else
      echo "No active session in $CWD. Nothing to reset."
    fi
