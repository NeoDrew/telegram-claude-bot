# commands/close_delete.sh - handler for /close_delete
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # "✕ close" button target for a menu that menu_new.sh spawned as a
    # fresh message (the turn-end footer's "☰ Menu" button). Unlike the
    # ordinary act:close (close.sh), which COLLAPSES the home-base
    # menu to a small reopenable stub, this one deletes the message
    # outright: a footer-spawned menu is a one-off lookup sitting right
    # under a footer that ALREADY carries its own permanent "☰ Menu"
    # button, so collapsing here instead of deleting left a second,
    # redundant "Menu" stub next to it forever, one more copy every time
    # the footer's button was tapped. Deleting returns the
    # chat to exactly one menu entry point per footer, the footer's own.
    #
    # Emit nothing on stdout, same reasoning as close.sh: the listener's
    # "if reply non-empty, edit/send" path would otherwise turn an empty
    # close into a stranded text message.
    if [[ -n "${CALLBACK_MSG_ID:-}" ]]; then
      CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" delete "$CALLBACK_MSG_ID" >/dev/null 2>&1 || true
    fi
