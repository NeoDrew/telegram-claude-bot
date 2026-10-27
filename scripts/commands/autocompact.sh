# commands/autocompact.sh - per-room auto-compact threshold.
# SOURCED by handle_action.sh: REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers are in scope.
#
# Modes:
#   autocompact            → draw the picker (40% to 90% in steps of 5)
#   autocompact <pct>      → store the threshold, redraw the picker
#   autocompact default    → clear it (Claude Code's own threshold)
#
# Stored as state/rooms/<chat>/autocompact (a single integer). spawn_claude.sh
# exports it as CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, which Claude Code reads as
# "compact once context reaches this percent of the window". The listener's
# stale-spawn check counts this file's mtime, so a change lands on the next
# message (fresh process, session preserved), no restart.

    AC_FILE="$ROOM_DIR/autocompact"
    AC_MIN=40; AC_MAX=90; AC_STEP=5

    if [[ -n "$ARGS" ]]; then
      VAL=$(echo "$ARGS" | awk '{print tolower($1)}' | tr -d '%')
      if [[ "$VAL" == "default" || "$VAL" == "none" || "$VAL" == "reset" ]]; then
        rm -f "$AC_FILE"
      elif [[ "$VAL" =~ ^[0-9]+$ ]] && (( VAL >= AC_MIN && VAL <= AC_MAX )); then
        echo "$VAL" > "$AC_FILE"
      else
        echo "Invalid auto compact threshold '$ARGS'. Pick ${AC_MIN} to ${AC_MAX}, or default."
        exit 0
      fi
    fi

    CUR=$(tr -d ' \n\r' < "$AC_FILE" 2>/dev/null || echo "")
    KB=$(CUR="$CUR" AC_MIN=$AC_MIN AC_MAX=$AC_MAX AC_STEP=$AC_STEP python3 <<'PYEOF'
import json, os
cur = os.environ.get("CUR", "")
lo, hi, step = int(os.environ["AC_MIN"]), int(os.environ["AC_MAX"]), int(os.environ["AC_STEP"])
def b(label, arg, sel):
    return {"text": ("✅ " if sel else "") + label, "callback_data": f"act:autocompact {arg}"}
btns = [b(f"{p}%", p, cur == str(p)) for p in range(lo, hi + 1, step)]
rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
rows.append([b("default", "default", not cur)])
print(json.dumps(rows), end="")
PYEOF
)
    if [[ -n "$CUR" ]]; then
      STATE="Current: **${CUR}%**. $(bot_name) compacts automatically once context reaches ${CUR}% of the window."
    else
      STATE="Current: **default** (Claude Code's own threshold, near the end of the window)."
    fi
    BODY="**🗜 Auto compact**

When context fills past the chosen level, the conversation is summarised automatically. Lower means compacting earlier and more often.

${STATE}"
    [[ -n "$ARGS" ]] && BODY="${BODY}
Takes effect on your next message, session preserved."
    reply_kb "$BODY" "$KB"
