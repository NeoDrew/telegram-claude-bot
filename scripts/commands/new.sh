# commands/new.sh - handler for /new
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    RS="$REPO_ROOT/scripts/room_state.py"
    CWD=$(python3 "$RS" get-cwd "$SESSIONS_FILE")
    if [[ -n "$(python3 "$RS" get-session "$SESSIONS_FILE")" ]]; then
      python3 "$RS" clear-session "$SESSIONS_FILE"
      kill_room_spawn "$ROOM_DIR" 1
      echo "New session started in $CWD. Next message begins fresh context."
    else
      echo "No active session in $CWD. Nothing to reset."
    fi
