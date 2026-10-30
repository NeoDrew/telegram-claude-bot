# commands/verbosity.sh - handler for the "verbosity" action.
#
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, CMD, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers (reply_kb, ...) are all in scope.
#
# Modes:
#   verbosity                  → draw the settings screen
#   verbosity <key> <value>    → set the value, then redraw the screen
#
# State lives in state/rooms/<chat>/verbosity (managed by verbosity.py).

    if [[ -n "$ARGS" ]]; then
      VKEY=$(echo "$ARGS" | awk '{print $1}')
      VVAL=$(echo "$ARGS" | awk '{print $2}')
      if ! python3 "$REPO_ROOT/scripts/verbosity.py" set "$ROOM_CHAT_ID" "$VKEY" "$VVAL" >/dev/null 2>&1; then
        # Bad input shouldn't be reachable from the menu buttons (they
        # only fire valid combinations), but be loud if it does.
        reply_kb "Invalid verbosity setting: $VKEY=$VVAL." "[]"
        exit 0
      fi
    fi

    # Render the screen: short explanation + current-state summary + the
    # toggle keyboard with ● marking each active value.
    SUMMARY=$(python3 "$REPO_ROOT/scripts/verbosity.py" summary "$ROOM_CHAT_ID")
    KB=$(python3 "$REPO_ROOT/scripts/verbosity.py" render-kb "$ROOM_CHAT_ID")

    BODY="**🔊 Verbosity**

What gets streamed back during a claude turn:
• **Tool calls**: _full_ sends each one with its command or file, _compact_ lists just the names in one message (⚙️ Bash, ⚙️ Bash, 📖 Read)
• **Edits**: file changes (Edit/Write/MultiEdit); _full diff_ sends the red/green block, _marker only_ just names the file, _compact_ adds 📝 Edit to the compact list
• **Thinking**: the \"💭 Thinking…\" placeholder at turn start

Current: $SUMMARY"

    reply_kb "$BODY" "$KB"
