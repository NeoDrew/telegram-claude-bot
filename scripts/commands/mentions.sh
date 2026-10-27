# commands/mentions.sh - toggle the per-room "@-mentions only" gate.
# SOURCED by handle_action.sh: REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, CALLBACK_MSG_ID
# and lib.sh helpers are in scope. Output the reply on stdout; the listener
# wraps it with a ⬅ Menu row.
#
# When ON, in a GROUP chat the bot only acts on messages that @-mention it; ordinary
# chatter is ignored (the gate lives in listener.sh::dispatch, which reads this
# file live, so the toggle takes effect on the next message, no restart). DMs
# ignore the setting. Stored as state/rooms/<chat>/mentions_only: the single
# word "on" enables it; absent = off. Mirrors effort.sh's file style.

    CUR=$(tr -d ' \n\r' < "$ROOM_DIR/mentions_only" 2>/dev/null || echo "")
    if [[ "$CUR" == "on" ]]; then
      rm -f "$ROOM_DIR/mentions_only"
      echo "🔔 **@-mentions only: off** for this chat. $(bot_name) will respond to every message here again."
    else
      echo "on" > "$ROOM_DIR/mentions_only"
      if [[ "$ROOM_CHAT_ID" == -* ]]; then
        echo "🔔 **@-mentions only: ON** for this chat. $(bot_name) now ignores messages that don't @ it here. Takes effect on the next message, no restart."
      else
        echo "🔔 **@-mentions only: ON**, but note this only affects GROUP chats. In a direct chat $(bot_name) always replies."
      fi
    fi
