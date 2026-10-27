# commands/close.sh - handler for /close
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # "× close menu" button target. Collapses the menu down to a single
    # "☰ Menu" button in place, rather than deleting the message (the
    # earlier behaviour). Deleting threw away the reader's place in the
    # chat history; collapsing keeps a one-tap way back to the full menu
    # right where it was, the same "☰ Menu" affordance the turn-end
    # footer buttons use, and tapping it re-expands via the existing
    # act:menu handler (menu.sh), which edits this same message back to
    # the full screen.
    #
    # The collapsed message carries the text "Menu", matching the button
    # below it, so the bubble reads as one label+action unit. Telegram's
    # sendMessage rejects empty text and blocks invisible fillers too (a
    # zero-width space 400s with "text must be non-empty", a plain space
    # 400s the same way after being trimmed), and a bare filler character
    # such as a middle dot reads as a stray glyph.
    reply_kb "Menu" '[[{"text": "☰ Menu", "callback_data": "act:menu"}]]' 0
