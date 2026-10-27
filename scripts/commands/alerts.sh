# commands/alerts.sh - bind/unbind the global ALERTS channel.
# SOURCED by handle_action.sh: REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers are in scope. Output the reply on
# stdout; the listener wraps it with a ⬅ Menu row.
#
# The alerts channel is the ONE chat the owner keeps unmuted. Urgent pings
# (`notify-cmd --alert`, the assistant's escalation path) land there; every other room
# can be muted. Binding is stored at state/.alerts_chat_id (GLOBAL, one per
# bot; data not code, discovered at runtime per the design axiom). Unbound →
# --alert falls back to the owner DM.

    ALERTS_FILE="$REPO_ROOT/state/.alerts_chat_id"
    SUB=$(echo "$ARGS" | awk '{print tolower($1)}')

    bound_id() { tr -d ' \n\r' < "$ALERTS_FILE" 2>/dev/null || true; }
    room_label() {  # label for a chat id, falling back to the bare id
      local id="$1" lbl=""
      lbl=$(tr -d '\n\r' < "$REPO_ROOT/state/rooms/$id/label" 2>/dev/null || true)
      [[ -n "$lbl" ]] && echo "$lbl" || echo "room $id"
    }

    case "$SUB" in
      here)
        if [[ "$ROOM_CHAT_ID" != -* ]]; then
          echo "This is a DM, which is already the fallback when no alerts channel is bound. Pick a GROUP chat (one you'll keep unmuted) and tap 🚨 alerts there."
        else
          printf '%s\n' "$ROOM_CHAT_ID" > "$ALERTS_FILE"
          echo "🚨 **This room ($(room_label "$ROOM_CHAT_ID")) is now the alerts channel.** Keep it unmuted; urgent pings from every project and room land here (notify-cmd --alert). Everything else is safe to mute."
        fi
        ;;
      off)
        if [[ -s "$ALERTS_FILE" ]]; then
          PREV=$(bound_id)
          rm -f "$ALERTS_FILE"
          echo "🚨 Alerts channel unbound (was $(room_label "$PREV")). Urgent pings fall back to your DM."
        else
          echo "No alerts channel was bound. Urgent pings go to your DM."
        fi
        ;;
      *)
        CUR=$(bound_id)
        if [[ -n "$CUR" ]]; then
          if [[ "$CUR" == "$ROOM_CHAT_ID" ]]; then
            TXT="🚨 **Alerts channel: THIS room.** Keep it unmuted; urgent pings from every project land here."
          else
            TXT="🚨 **Alerts channel: $(room_label "$CUR").** Urgent pings land there; this room can stay muted."
          fi
        else
          TXT="🚨 **No alerts channel bound.** Urgent pings (notify-cmd --alert) fall back to your DM. Bind a dedicated unmuted group so the DM stays a clean workspace."
        fi
        KB='[['
        if [[ "$ROOM_CHAT_ID" == -* && "$CUR" != "$ROOM_CHAT_ID" ]]; then
          KB+='{"text":"🚨 make THIS room the alerts channel","callback_data":"act:alerts here"}'
          [[ -n "$CUR" ]] && KB+=','
        fi
        if [[ -n "$CUR" ]]; then
          KB+='{"text":"🗑 unbind (fall back to DM)","callback_data":"act:alerts off"}'
        fi
        KB+=']]'
        [[ "$KB" == '[[]]' ]] && KB='[[{"text":"ℹ️ bind from a group chat","callback_data":"decor:alerts-dm"}]]'
        reply_kb "$TXT" "$KB"
        ;;
    esac
