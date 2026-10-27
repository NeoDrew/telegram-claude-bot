# commands/noop.sh - handler for /noop
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Cancel-button target for every confirm prompt and the /usage
    # Dismiss button. Deletes the source message outright rather than
    # editing it to "(cancelled)": leaving a stub line in chat was
    # noise. Same delete-on-tap pattern as /close.
    if [[ -n "${CALLBACK_MSG_ID:-}" ]]; then
      CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" delete "$CALLBACK_MSG_ID" >/dev/null 2>&1 || true
    fi
