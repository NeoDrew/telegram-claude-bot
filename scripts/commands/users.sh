# commands/users.sh - per-room user management: Members + Pending, with
# one-tap moves between the two lists.
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, CMD, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers (reply_kb, extract_var, ...) are in scope.
#
# The bot OWNER (TELEGRAM_CHAT_ID) is always allowed everywhere and is never
# listed. This screen manages EXTRA users for THIS room (per-room scope), in
# two sections the buttons move people between:
#   ✅ Members: approved, can talk to the bot. 🗑 demotes one to Pending.
#   ⏳ Pending: messaged the bot but not allowed yet (captured by the listener's
#                drop path). ✅ promotes one to Members.
# Backed by approved_users.py; the listener reads the approved list live, so a
# move takes effect on the user's next message with no restart.
#
# Verbs (callback_data "act:users ..."):
#   users            → render the two-section screen
#   users add        → pop the native contact picker (secondary; flaky for
#                      group-only members, hence the Pending flow is primary)
#   users approve U [N] → approve U (resolve @username if N absent), drop from
#                      pending, re-render. Also called by the listener when the
#                      contact picker returns a users_shared event.
#   users rm U       → revoke U, demote to pending, re-render

    SUB=$(echo "$ARGS" | awk '{print tolower($1)}')
    BOT_TOKEN=$(extract_var TELEGRAM_BOT_TOKEN)
    AU="$REPO_ROOT/scripts/approved_users.py"

    case "$SUB" in
      add)
        # The picker is a reply keyboard; Telegram tracks one per chat, so it
        # replaces the persistent "📋 Menu" one, so drop the marker and the next
        # /menu open restores it.
        rm -f "$ROOM_DIR/.reply_kb_attached"
        CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" request_user_kb \
          "👇 Pick a contact to approve for this chat. If the picker comes up empty, just have them message $(bot_name) once; they'll appear under ⏳ Pending to approve with one tap." \
          "${CALLBACK_MSG_ID:-}" >/dev/null 2>&1 || true
        exit 0
        ;;
      approve)
        TARGET_UID=$(echo "$ARGS" | awk '{print $2}')
        TARGET_UNAME=$(echo "$ARGS" | awk '{print $3}')
        if [[ ! "$TARGET_UID" =~ ^[1-9][0-9]*$ ]]; then
          reply_kb "⚠️ Couldn't read that user id. Open 👥 users and try again." '[]'
          exit 0
        fi
        if [[ "$TARGET_UID" == "$(extract_var TELEGRAM_CHAT_ID)" ]]; then
          reply_kb "ℹ️ That's you; the owner is always allowed in every chat." '[]'
          exit 0
        fi
        # Pending approvals arrive without an @username (callback carries only
        # the id); resolve it best-effort so the roster reads nicely.
        if [[ -z "$TARGET_UNAME" && -n "$BOT_TOKEN" ]]; then
          TARGET_UNAME=$(BOT="$BOT_TOKEN" CID="$ROOM_CHAT_ID" U="$TARGET_UID" \
              API_BASE="${TELEGRAM_API_BASE:-https://api.telegram.org}" python3 - <<'PY'
import os, json, urllib.request
try:
    url = (f"{os.environ['API_BASE']}/bot{os.environ['BOT']}"
           f"/getChatMember?chat_id={os.environ['CID']}&user_id={os.environ['U']}")
    with urllib.request.urlopen(url, timeout=5) as r:
        d = json.load(r)
    print((d.get("result") or {}).get("user", {}).get("username") or "")
except Exception:
    print("")
PY
)
        fi
        python3 "$AU" add "$ROOM_CHAT_ID" "$TARGET_UID" "$TARGET_UNAME" >/dev/null 2>&1
        python3 "$AU" pending-remove "$ROOM_CHAT_ID" "$TARGET_UID" >/dev/null 2>&1
        # fall through → re-render (they now sit under ✅ Members)
        ;;
      rm|remove)
        TARGET_UID=$(echo "$ARGS" | awk '{print $2}')
        if [[ "$TARGET_UID" =~ ^[1-9][0-9]*$ ]]; then
          python3 "$AU" remove "$ROOM_CHAT_ID" "$TARGET_UID" >/dev/null 2>&1
          python3 "$AU" pending-add "$ROOM_CHAT_ID" "$TARGET_UID" >/dev/null 2>&1
        fi
        # fall through → re-render (they now sit under ⏳ Pending)
        ;;
    esac

    # Render the two-section screen. Built in python so usernames can't break
    # the keyboard JSON; pending users have only an id stored, so resolve their
    # @handle via getChatMember at render time (best-effort, capped list).
    USERS_OUT=$(REPO_ROOT="$REPO_ROOT" ROOM_CHAT_ID="$ROOM_CHAT_ID" BOT_TOKEN="$BOT_TOKEN" \
        TELEGRAM_API_BASE="${TELEGRAM_API_BASE:-https://api.telegram.org}" python3 <<'PYEOF'
import json, os, sys, urllib.request
repo = os.environ['REPO_ROOT']
cid = os.environ['ROOM_CHAT_ID']
bot = os.environ.get('BOT_TOKEN', '')
api_base = os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org')
sys.path.insert(0, os.path.join(repo, 'scripts'))
import approved_users
import bot_config
name = bot_config.bot_name()


def resolve(uid):
    if not bot:
        return ''
    try:
        url = (f"{api_base}/bot{bot}"
               f"/getChatMember?chat_id={cid}&user_id={uid}")
        with urllib.request.urlopen(url, timeout=4) as r:
            d = json.load(r)
        u = (d.get('result') or {}).get('user') or {}
        if u.get('username'):
            return '@' + u['username']
        return ((u.get('first_name') or '') + ' ' + (u.get('last_name') or '')).strip()
    except Exception:
        return ''


members = approved_users.list_room(cid)
pending = approved_users.pending_list_room(cid)

lines = ["**👥 Users · this chat**",
         "_You (the owner) are always allowed everywhere and aren't listed._", ""]
rows = []

lines.append(f"**✅ Members** · can talk to {name}")
if members:
    for u in members:
        uname = u.get('username') or ''
        uid = u.get('id')
        disp = f"@{uname}" if uname else f"user {uid}"
        lines.append(f"• {disp}")
        rows.append([{"text": f"🗑 {disp}", "callback_data": f"act:users rm {uid}"}])
else:
    lines.append("_none yet_")

lines.append("")
lines.append(f"**⏳ Pending** · messaged {name}, not allowed yet")
if pending:
    for u in pending:
        uid = u.get('id')
        disp = resolve(uid) or f"user {uid}"
        lines.append(f"• {disp}")
        rows.append([{"text": f"✅ approve {disp}", "callback_data": f"act:users approve {uid}"}])
else:
    lines.append(f"_none waiting; when someone messages {name} here, they show up to approve_")

rows.append([{"text": "➕ add by contact", "callback_data": "act:users add"}])
print("\n".join(lines) + "\x1f" + json.dumps(rows))
PYEOF
)
    reply_kb "${USERS_OUT%%$'\x1f'*}" "${USERS_OUT#*$'\x1f'}"
