# commands/label.sh - handler for /label
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Set or read the friendly name for this room. Telegram returns the
    # other-end-of-the-chat's name for DMs (your own username for your DM
    # with the bot), which is rarely what you mean when you say "this
    # chat is called Project X". /label gives you authority to override.
    #
    # Tap-to-rename UX: bare /label arms a marker file `.awaiting_label`.
    # The listener checks that marker BEFORE routing free-text messages
    # to claude: if armed, the next text message is consumed as the
    # new label instead of being sent to claude. Avoids forcing the
    # user to type the literal "/label" prefix every time.
    CUR=$(cat "$ROOM_DIR/label" 2>/dev/null || echo "(none)")
    if [[ -z "$ARGS" ]]; then
      touch "$ROOM_DIR/.awaiting_label"
      # Cancel button fires /menu which (a) is a slash command so the
      # listener's tap-to-rename interceptor clears `.awaiting_label`
      # and falls through, (b) re-renders the main menu in place.
      KB='[[{"text":"× cancel","callback_data":"act:menu"}]]'
      reply_kb "**🏷 Rename room** · current: \`$CUR\`

Just reply with the new name. Your next message becomes the label." "$KB" 0
      exit 0
    fi
    # Trim whitespace and write
    NEW=$(echo "$ARGS" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    if [[ -z "$NEW" ]]; then
      echo "Empty label refused."
      exit 0
    fi
    echo "$NEW" > "$ROOM_DIR/label"
    # Pin it: a user-chosen label is authoritative and must NOT be silently
    # overwritten by the listener's auto-sync of the live Telegram title.
    touch "$ROOM_DIR/.label_pinned"
    rm -f "$ROOM_DIR/.awaiting_label"
    echo "Room label: \`$CUR\` → \`$NEW\`"
