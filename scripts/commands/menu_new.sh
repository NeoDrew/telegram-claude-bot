# commands/menu_new.sh - handler for /menu_new
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Opens the menu as a NEW message instead of editing the tapped
    # message in place. The turn-end footer's "☰ Menu" button (see
    # stream_to_telegram.py's tg_send keyboard param) uses this verb
    # specifically, NOT the ordinary act:menu: every other menu button
    # editing its own source message in place is exactly right (that's
    # the whole point of the menu/tune/more navigation), but the footer's
    # "⏱ Worked for..." text is real content worth keeping, not
    # a throwaway screen to be overwritten. Reusing act:menu there
    # destroyed the turn's duration/context summary the instant Menu was
    # tapped. Unsetting CALLBACK_MSG_ID before
    # sourcing the real menu.sh makes reply_kb fall through to its own
    # "no CALLBACK_MSG_ID, send fresh" branch (see lib.sh), so the footer
    # survives untouched and the menu lands as its own message right
    # below it, same as tapping the menu from a cold chat.
    unset CALLBACK_MSG_ID
    # This spawned-from-a-footer menu is a one-off lookup, not the
    # home-base menu, so its own close button should delete it outright
    # rather than collapse to a stub (see menu.sh's MENU_CLOSE_VERB
    # comment for the full reasoning: collapsing here left a second,
    # permanent "☰ Menu" button sitting right under the footer's own one).
    export MENU_CLOSE_VERB="close_delete"
    export MENU_CLOSE_LABEL="✕ close"
    . "$REPO_ROOT/scripts/commands/menu.sh"
