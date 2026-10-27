# commands/cancel.sh - handler for the "cancel" action.
#
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, SESSIONS_FILE,
# BUSY_LOCK, QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers (reply_kb, kill_room_spawn, is_busy)
# are all in scope.

    if is_busy "$ROOM_CHAT_ID"; then
      kill_room_spawn "$ROOM_DIR" 1
      : > "$QUEUE_FILE"
      reply_kb "🛑 Cancelled. Queue and inbox cleared." "[]"
    else
      rm -f "$BUSY_LOCK"
      reply_kb "Nothing in flight to cancel." "[]"
    fi
