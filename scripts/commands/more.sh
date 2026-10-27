# commands/more.sh - the "⚙️ more options" submenu.
# SOURCED by handle_action.sh: REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID and lib.sh
# helpers are in scope.
#
# Holds the config-rare actions so the main menu stays lean. Everything
# here is one extra tap; reply_kb appends the ⬅ Menu row automatically.

    MENTIONS_ROW=""
    if [[ "$ROOM_CHAT_ID" == -* ]]; then
      MO=$(tr -d ' \n\r' < "$ROOM_DIR/mentions_only" 2>/dev/null || echo "")
      [[ "$MO" == "on" ]] && MO_LBL="🔔 @-mentions only: ON" || MO_LBL="🔔 @-mentions only: off"
      MENTIONS_ROW=",[{\"text\":\"${MO_LBL}\",\"callback_data\":\"act:mentions\"}]"
    fi

    ABOUND=$(tr -d ' \n\r' < "$REPO_ROOT/state/.alerts_chat_id" 2>/dev/null || echo "")
    if [[ -n "$ABOUND" && "$ABOUND" == "$ROOM_CHAT_ID" ]]; then
      ALERTS_LBL="🚨 alerts: HERE"
    elif [[ -n "$ABOUND" ]]; then
      ALERTS_LBL="🚨 alerts"
    else
      ALERTS_LBL="🚨 alerts: DM"
    fi

    AC=$(tr -d ' \n\r' < "$ROOM_DIR/autocompact" 2>/dev/null || echo "")
    [[ -n "$AC" ]] && AC_LBL="🗜 auto compact: ${AC}%" || AC_LBL="🗜 auto compact: default"

    KB='[
      [{"text":"🌐 rooms","callback_data":"act:rooms"},
       {"text":"🔖 chrome tab","callback_data":"act:tab"}],
      [{"text":"📅 schedule","callback_data":"act:schedule"},
       {"text":"🏷 label","callback_data":"act:label"}],
      [{"text":"🔊 verbosity","callback_data":"act:verbosity"},
       {"text":"👥 users","callback_data":"act:users"}],
      [{"text":"🧩 update plugins","callback_data":"act:plugin update-all"},
       {"text":"🔐 login","callback_data":"act:login"}],
      [{"text":"'"$ALERTS_LBL"'","callback_data":"act:alerts"},
       {"text":"♻️ restart","callback_data":"act:restart"}],
      [{"text":"'"$AC_LBL"'","callback_data":"act:autocompact"}]'"$MENTIONS_ROW"'
    ]'
    reply_kb "**⚙️ More options**" "$KB"
