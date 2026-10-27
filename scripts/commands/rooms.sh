# commands/rooms.sh - handler for /rooms
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Global dashboard: every Telegram chat the bot has state for, with a 🗑
    # forget button per room. Rendering lives in scripts/render_rooms.py so
    # /rooms and the forget flow show the identical view. reply_kb appends the
    # ⬅ Menu row, which doubles as "cancel"; there is no separate cancel.
    OUT=$(REPO_ROOT="$REPO_ROOT" CURRENT_ROOM="$ROOM_CHAT_ID" \
          TELEGRAM_BOT_TOKEN="$(extract_var TELEGRAM_BOT_TOKEN)" \
          python3 "$REPO_ROOT/scripts/render_rooms.py")
    HEAD="${OUT%%$'\x1f'*}"
    KB="${OUT#*$'\x1f'}"
    reply_kb "$HEAD" "$KB"
