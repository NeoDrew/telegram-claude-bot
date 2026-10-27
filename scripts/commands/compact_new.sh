# commands/compact_new.sh - handler for /compact_new
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Same problem as menu_new.sh, for the turn-end footer's "🗜 Compact"
    # button: plain act:compact's reply is a stdout string, and
    # listener.sh's invoke_action wraps that by EDITING CALLBACK_MSG_ID
    # in place, which on the footer destroys the "⏱ Worked for..." text
    # the same way act:menu did (same root cause, different code
    # path). menu_new.sh's fix (just "unset
    # CALLBACK_MSG_ID") doesn't carry over here: that edit-vs-send
    # decision is made by invoke_action back in the PARENT listener.sh
    # process, using ITS OWN copy of the variable, not this subprocess's,
    # so unsetting it in this shell would never reach it. Instead, run
    # compact.sh in a subshell to capture its reply text, then send that
    # text ourselves via reply_kb, which DOES decide edit-vs-send off
    # THIS subprocess's own CALLBACK_MSG_ID, so clearing it here actually
    # works, and the reply lands as a fresh message below the footer.
    unset CALLBACK_MSG_ID
    COMPACT_REPLY=$(. "$REPO_ROOT/scripts/commands/compact.sh")
    reply_kb "$COMPACT_REPLY" '[]' 1
