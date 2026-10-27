# commands/forget.sh - handler for /forget
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Delete a room's local state (groups the bot was kicked from, leftover
    # test groups). You can't forget the room you're sending from.
    #
    # The room list + 🗑 buttons live in the unified /rooms dashboard
    # (scripts/render_rooms.py), so bare /forget shows that same dashboard.
    # A 🗑 button fires "/forget ask <id>" (confirm), "/forget yes <id>"
    # executes. There is NO × cancel/done button anywhere: reply_kb appends a
    # ⬅ Menu row, and that single button is the way out / back to the menu.

    # Render the shared rooms dashboard (optionally with a "just forgot" note)
    # and send it with reply_kb so the 🗑 buttons + ⬅ Menu come along.
    render_rooms() {
      local out
      out=$(REPO_ROOT="$REPO_ROOT" CURRENT_ROOM="$ROOM_CHAT_ID" \
            TELEGRAM_BOT_TOKEN="$(extract_var TELEGRAM_BOT_TOKEN)" \
            FORGOT_NOTE="${1:-}" \
            python3 "$REPO_ROOT/scripts/render_rooms.py")
      reply_kb "${out%%$'\x1f'*}" "${out#*$'\x1f'}"
    }

    if [[ -z "$ARGS" ]]; then
      render_rooms
      exit 0
    fi

    # "yes <id>" or "<id>" → execute immediately.
    SUB=$(echo "$ARGS" | awk '{print tolower($1)}')
    if [[ "$SUB" == "yes" ]]; then
      TARGET=$(echo "$ARGS" | awk '{print $2}')
    else
      TARGET="$ARGS"
    fi

    if [[ -z "$TARGET" ]]; then
      echo "Pick the chat to forget from 🌐 rooms in ⚙️ more options."
      exit 0
    fi
    if [[ "$TARGET" == "$ROOM_CHAT_ID" ]]; then
      echo "Refused: can't forget the room you're sending from."
      exit 0
    fi
    TARGET_DIR="$REPO_ROOT/state/rooms/$TARGET"
    if [[ ! -d "$TARGET_DIR" ]]; then
      echo "No room with id \`$TARGET\` tracked."
      exit 0
    fi
    LABEL_FORGOT=$(cat "$TARGET_DIR/label" 2>/dev/null || echo "$TARGET")

    # Delete, then re-render the dashboard so the user stays in
    # the combined view and can forget another. paint_cancel=0: no chat to
    # paint into (the room is being removed).
    kill_room_spawn "$TARGET_DIR" 0
    rm -rf "$TARGET_DIR"
    render_rooms "Forgotten \`$LABEL_FORGOT\`"
