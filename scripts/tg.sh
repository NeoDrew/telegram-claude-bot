#!/bin/bash
# tg.sh - Telegram Bot API wrapper for the central claude bot.
#
# Subcommands:
#   send     "<text>"                 → prints message_id to stdout
#   edit     <msg_id> "<text>"        → returns 0 on success
#   photo    <path>  [<caption>]      → prints message_id to stdout
#   document <path>  [<caption>]      → prints message_id to stdout
#
# Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the repo's .env (or env
# vars if already set). Sends with parse_mode=HTML, auto-escaping content
# outside <pre>...</pre> blocks so tabular output renders monospace.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

. "$REPO_ROOT/scripts/lib.sh"

if [[ -f "$REPO_ROOT/.env" ]]; then
  TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(extract_var TELEGRAM_BOT_TOKEN)}"
  TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(extract_var TELEGRAM_CHAT_ID)}"
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "tg.sh: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing" >&2
  exit 2
fi

# Per-call routing override. The listener exports CHAT_ID when dispatching a
# message so replies (slash-command replies, queued/cancel acks, photo
# uploads, media groups) land in the originating chat, not always the bot
# owner's DM. Defaults to TELEGRAM_CHAT_ID (the DM) for backwards compat.
TARGET_CHAT_ID="${CHAT_ID:-$TELEGRAM_CHAT_ID}"

# Overridable so the E2E test harness (tests/e2e/) can point this at a local
# fake Bot API server. Defaults to the real endpoint. Inherited from whatever
# spawned this process (listener.sh / spawn_claude.sh already export it), so
# notify-cmd's real tg.sh child picks it up too without needing its own edit.
TELEGRAM_API_BASE="$(bot_config TELEGRAM_API_BASE https://api.telegram.org)"
API="${TELEGRAM_API_BASE}/bot${TELEGRAM_BOT_TOKEN}"

# Append a (timestamp, chat_id, message_id) row to the per-room sent-log so
# /clear can deleteMessage them later. Bots can only delete messages they
# sent themselves and only within 48h, so we also keep the log small.
log_sent_msg() {
  local mid="$1"
  [[ -z "$mid" || "$mid" == "null" ]] && return 0
  local repo_root="$REPO_ROOT"
  local room_dir="$repo_root/state/rooms/$TARGET_CHAT_ID"
  mkdir -p "$room_dir" 2>/dev/null
  local log_file="$room_dir/sent_msgs.log"
  printf '%s\t%s\n' "$(date +%s)" "$mid" >> "$log_file"
  # Prune entries older than 48h, bots can't delete messages beyond that
  # window, so older entries are only wasting disk.
  local cutoff=$(( $(date +%s) - 172800 ))
  awk -v c="$cutoff" '$1 > c' "$log_file" > "$log_file.tmp" 2>/dev/null \
    && mv "$log_file.tmp" "$log_file" \
    || rm -f "$log_file.tmp"
}

# Format text for Telegram HTML mode. Delegates to scripts/md_telegram.py so
# the rendering matches stream_to_telegram.py exactly (same markdown subset,
# same truncation rule).
format_html() {
  RAW="$1" SCRIPTS_DIR="$REPO_ROOT/scripts" python3 <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ['SCRIPTS_DIR'])
from md_telegram import md_to_telegram_html
print(md_to_telegram_html(os.environ.get('RAW', '')), end='')
PYEOF
}

extract_msg_id() {
  RESP="$1" python3 <<'PYEOF'
import os, json, sys
try:
    d = json.loads(os.environ.get('RESP', ''))
except Exception as e:
    sys.stderr.write(f'tg.sh: invalid json: {e}\n')
    sys.exit(1)
if not d.get('ok'):
    sys.stderr.write(f'tg.sh: api error: {d}\n')
    sys.exit(1)
print(d['result']['message_id'])
PYEOF
}

ok_or_die() {
  RESP="$1" python3 <<'PYEOF'
import os, json, sys
try:
    d = json.loads(os.environ.get('RESP', ''))
except Exception as e:
    sys.stderr.write(f'tg.sh: invalid json: {e}\n')
    sys.exit(1)
if not d.get('ok'):
    # "message is not modified" is Telegram's 400 when an edit's content
    # exactly matches what's already on the message. Treating it as failure
    # would make the caller fall back to a fresh sendMessage and DUPLICATE
    # the message in the chat, caller intent was "make the message show X",
    # and it already does. Silently succeed.
    desc = (d.get('description') or '').lower()
    if 'not modified' in desc:
        sys.exit(0)
    sys.stderr.write(f'tg.sh: api error: {d}\n')
    sys.exit(1)
PYEOF
}

cmd_send() {
  local text="$1"
  local body
  body=$(format_html "$text")
  local resp
  resp=$(curl -s --max-time 20 -X POST "${API}/sendMessage" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${body}")
  local mid
  mid=$(extract_msg_id "$resp")
  log_sent_msg "$mid"
  echo "$mid"
}

cmd_edit() {
  local mid="$1"
  local text="$2"
  local body
  body=$(format_html "$text")
  # Always clear inline keyboards on edit. The only callers that hit this
  # are handle_action.sh action replies and listener.sh's callback path,
  # both want the buttons gone after the user has chosen something so the
  # chat doesn't accumulate stale tap targets.
  local resp
  resp=$(curl -s --max-time 20 -X POST "${API}/editMessageText" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "message_id=${mid}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${body}" \
    --data-urlencode 'reply_markup={"inline_keyboard":[]}')
  ok_or_die "$resp"
}

# edit_html: like edit but skips the markdown→HTML conversion.
# Use when the caller already produces valid Telegram HTML (e.g. ask_answer.sh,
# the listener free-text intercept) and must not be double-processed.
cmd_edit_html() {
  local mid="$1"
  local text="$2"
  local resp
  resp=$(curl -s --max-time 20 -X POST "${API}/editMessageText" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "message_id=${mid}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${text}" \
    --data-urlencode 'reply_markup={"inline_keyboard":[]}')
  ok_or_die "$resp"
}

cmd_photo() {
  local path="$1"
  local caption="${2:-}"
  if [[ ! -f "$path" ]]; then
    echo "tg.sh: photo file not found: $path" >&2
    exit 1
  fi
  local resp
  if [[ -n "$caption" ]]; then
    resp=$(curl -s --max-time 60 -X POST "${API}/sendPhoto" \
      -F "chat_id=${TARGET_CHAT_ID}" \
      -F "caption=${caption}" \
      -F "photo=@${path}")
  else
    resp=$(curl -s --max-time 60 -X POST "${API}/sendPhoto" \
      -F "chat_id=${TARGET_CHAT_ID}" \
      -F "photo=@${path}")
  fi
  local mid
  mid=$(extract_msg_id "$resp")
  log_sent_msg "$mid"
  echo "$mid"
}

# Send any file as a Telegram document (unlike sendPhoto, no re-encoding and
# no size/dimension limits beyond Telegram's general bot-upload cap; the
# receiver gets the original bytes/filename back, right for zips, PDFs,
# code archives, anything sendPhoto would mangle).
cmd_document() {
  local path="$1"
  local caption="${2:-}"
  if [[ ! -f "$path" ]]; then
    echo "tg.sh: document file not found: $path" >&2
    exit 1
  fi
  local resp
  if [[ -n "$caption" ]]; then
    resp=$(curl -s --max-time 120 -X POST "${API}/sendDocument" \
      -F "chat_id=${TARGET_CHAT_ID}" \
      -F "caption=${caption}" \
      -F "document=@${path}")
  else
    resp=$(curl -s --max-time 120 -X POST "${API}/sendDocument" \
      -F "chat_id=${TARGET_CHAT_ID}" \
      -F "document=@${path}")
  fi
  local mid
  mid=$(extract_msg_id "$resp")
  log_sent_msg "$mid"
  echo "$mid"
}

# Send up to 10 photos as a single Telegram media group (album).
#
# Usage:
#   media_group <path>[::caption] <path>[::caption] ... <path>[::caption]
#
# A pair is just the absolute file path; the optional caption is appended
# after the literal "::" separator. Captions are truncated by Telegram to
# ~1024 chars per photo and only the first photo's caption shows in the
# album preview, but all captions appear under their photos when opened.
cmd_media_group() {
  local -a paths=()
  local -a captions=()
  local arg
  for arg in "$@"; do
    if [[ "$arg" == *"::"* ]]; then
      paths+=("${arg%%::*}")
      captions+=("${arg#*::}")
    else
      paths+=("$arg")
      captions+=("")
    fi
  done

  local n=${#paths[@]}
  if (( n < 2 || n > 10 )); then
    echo "tg.sh: media_group requires 2-10 photos (got $n)" >&2
    exit 1
  fi

  # Build the JSON media array and the -F attach@ args in parallel.
  local -a curl_args=(-F "chat_id=${TARGET_CHAT_ID}")
  local media_json
  media_json=$(
    PATHS="$(printf '%s\n' "${paths[@]}")" CAPTIONS="$(printf '%s\n' "${captions[@]}")" python3 <<'PYEOF'
import json, os
paths = os.environ['PATHS'].split('\n')
caps = os.environ['CAPTIONS'].split('\n')
out = []
for i, p in enumerate(paths):
    if not p:
        continue
    entry = {"type": "photo", "media": f"attach://p{i}"}
    cap = caps[i] if i < len(caps) else ''
    if cap:
        entry["caption"] = cap
    out.append(entry)
print(json.dumps(out), end='')
PYEOF
  )
  curl_args+=(-F "media=${media_json}")
  local i
  for (( i=0; i<n; i++ )); do
    if [[ ! -f "${paths[i]}" ]]; then
      echo "tg.sh: photo file not found: ${paths[i]}" >&2
      exit 1
    fi
    curl_args+=(-F "p${i}=@${paths[i]}")
  done

  local resp
  resp=$(curl -s --max-time 120 -X POST "${API}/sendMediaGroup" "${curl_args[@]}")
  # On success Telegram returns {"ok":true,"result":[<message>,...]}. We
  # don't print every message_id; the caller usually doesn't care.
  RESP="$resp" python3 <<'PYEOF'
import os, json, sys
try:
    d = json.loads(os.environ['RESP'])
except Exception as e:
    sys.stderr.write(f"tg.sh media_group: bad json: {e}\n  body={os.environ['RESP'][:300]}\n")
    sys.exit(1)
if not d.get('ok'):
    sys.stderr.write(f"tg.sh media_group: api error: {d}\n")
    sys.exit(1)
PYEOF
}

# Send a message with an inline keyboard. The keyboard is passed as JSON:
# a 2D array of {text, callback_data} rows. Example:
#   tg.sh kb "Pick effort:" \
#     '[[{"text":"low","callback_data":"act:effort low"},
#        {"text":"max","callback_data":"act:effort max"}]]'
# callback_data is up to 64 bytes per button. The wire format is always
# "act:<cmd>[ <args>]" for real actions or "decor:*" for inert buttons,
# the listener strips the "act:" prefix and routes the rest to
# handle_action.sh. When the user taps, the listener's getUpdates loop
# receives a callback_query update; see the cb branch in listener.sh.
cmd_kb() {
  local text="$1"
  local keyboard_json="$2"
  local body
  body=$(format_html "$text")
  local reply_markup
  reply_markup=$(KB="$keyboard_json" python3 <<'PYEOF'
import json, os, sys
kb = json.loads(os.environ['KB'])
print(json.dumps({"inline_keyboard": kb}))
PYEOF
)
  local resp
  resp=$(curl -s --max-time 20 -X POST "${API}/sendMessage" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${body}" \
    --data-urlencode "reply_markup=${reply_markup}")
  local mid
  mid=$(extract_msg_id "$resp")
  log_sent_msg "$mid"
  echo "$mid"
}

# Acknowledge a callback_query so the spinning loader on the button stops.
# Optional toast text shown briefly to the user.
cmd_answer_cb() {
  local cb_id="$1"
  local toast="${2:-}"
  local resp
  if [[ -n "$toast" ]]; then
    resp=$(curl -s --max-time 10 -X POST "${API}/answerCallbackQuery" \
      --data-urlencode "callback_query_id=${cb_id}" \
      --data-urlencode "text=${toast}")
  else
    resp=$(curl -s --max-time 10 -X POST "${API}/answerCallbackQuery" \
      --data-urlencode "callback_query_id=${cb_id}")
  fi
  # Don't fail on errors, the tap already happened. Silent ok-or-not.
  echo "$resp" >/dev/null
}

# Set a reaction emoji on a message. Used by the listener to ack
# "message received and queued" without spamming the chat. Silent on
# failure (reactions are best-effort UX).
cmd_react() {
  local mid="$1"
  local emoji="${2:-👀}"
  curl -s --max-time 10 -X POST "${API}/setMessageReaction" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "message_id=${mid}" \
    --data-urlencode "reaction=[{\"type\":\"emoji\",\"emoji\":\"${emoji}\"}]" \
    >/dev/null 2>&1 || true
}

# Delete a single message. Bots can only delete messages they sent, and
# only within 48h. Silent on failure, callers iterating a long list of
# message_ids don't want one stale id to break the whole sweep.
cmd_delete() {
  local mid="$1"
  curl -s --max-time 10 -X POST "${API}/deleteMessage" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "message_id=${mid}" >/dev/null 2>&1 || true
}

# Edit an existing message's text AND replace its inline keyboard in one call.
cmd_editkb() {
  local mid="$1"
  local text="$2"
  local keyboard_json="$3"
  local body reply_markup
  body=$(format_html "$text")
  reply_markup=$(KB="$keyboard_json" python3 <<'PYEOF'
import json, os, sys
try:
    kb = json.loads(os.environ['KB'])
    print(json.dumps({"inline_keyboard": kb}), end='')
except Exception as e:
    sys.stderr.write(f'tg.sh editkb: bad json: {e}\n')
    sys.exit(1)
PYEOF
)
  local resp
  resp=$(curl -s --max-time 20 -X POST "${API}/editMessageText" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "message_id=${mid}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${body}" \
    --data-urlencode "reply_markup=${reply_markup}")
  ok_or_die "$resp"
}

# Send a message that ATTACHES a persistent reply keyboard above the user's
# text input. Telegram remembers the reply keyboard per chat after a single
# sendMessage carries it in reply_markup, so callers only need to fire this
# once per (room, button-layout). Subsequent normal sendMessage calls (with
# inline keyboards or none) leave the reply keyboard intact.
#
# Usage: tg.sh show_reply_kb "<message text>"
#
# The keyboard is one row with a single 📋 Menu button. is_persistent keeps
# it visible across sessions; resize_keyboard makes it the right size for
# its content; selective=true (with reply_to_message_id when available)
# means in groups the keyboard only shows for the user who triggered it,
# not every group member.
cmd_show_reply_kb() {
  local text="$1"
  local reply_to="${2:-}"   # optional message_id to scope `selective` to
  local body reply_markup
  body=$(format_html "$text")
  reply_markup='{"keyboard":[[{"text":"📋 Menu"}]],"is_persistent":true,"resize_keyboard":true,"selective":true}'
  local extra=()
  if [[ -n "$reply_to" ]]; then
    extra+=(--data-urlencode "reply_parameters={\"message_id\":${reply_to},\"allow_sending_without_reply\":true}")
  fi
  curl -s --max-time 15 -X POST "${API}/sendMessage" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${body}" \
    --data-urlencode "reply_markup=${reply_markup}" \
    "${extra[@]}" >/dev/null 2>&1 || true
}

# Send a ONE-TIME reply keyboard whose single button opens Telegram's native
# contact picker (KeyboardButtonRequestUsers, Bot API 7.0+). When the owner
# picks a contact, Telegram replies with a `users_shared` service message that
# carries the chosen user's numeric id (+ @username, since request_username is
# set). The listener turns that into "users approve <id> <uname>". This is how
# the Users menu learns a friend's id without the friend having to message the
# bot first. one_time_keyboard hides it after one use; selective + reply_to
# scope the button to the owner in group chats.
#
# Usage: tg.sh request_user_kb "<prompt text>" [<reply_to_msg_id>]
cmd_request_user_kb() {
  local text="$1"
  local reply_to="${2:-}"
  local body
  body=$(format_html "$text")
  local reply_markup='{"keyboard":[[{"text":"👤 Choose a contact to approve","request_users":{"request_id":1,"user_is_bot":false,"request_name":true,"request_username":true,"max_quantity":1}}]],"one_time_keyboard":true,"resize_keyboard":true,"selective":true}'
  local extra=()
  if [[ -n "$reply_to" ]]; then
    extra+=(--data-urlencode "reply_parameters={\"message_id\":${reply_to},\"allow_sending_without_reply\":true}")
  fi
  curl -s --max-time 15 -X POST "${API}/sendMessage" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${body}" \
    --data-urlencode "reply_markup=${reply_markup}" \
    "${extra[@]}" >/dev/null 2>&1 || true
}

# Replace the inline keyboard on an existing message without changing its text.
# Used by the /menu back-button logic to re-attach a keyboard after dispatch.
cmd_setkb() {
  local mid="$1"
  local keyboard_json="$2"
  local reply_markup
  reply_markup=$(KB="$keyboard_json" python3 <<'PYEOF'
import json, os, sys
try:
    kb = json.loads(os.environ['KB'])
    print(json.dumps({"inline_keyboard": kb}), end='')
except Exception as e:
    sys.stderr.write(f'tg.sh setkb: bad json: {e}\n')
    sys.exit(1)
PYEOF
)
  curl -s --max-time 20 -X POST "${API}/editMessageReplyMarkup" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "message_id=${mid}" \
    --data-urlencode "reply_markup=${reply_markup}" >/dev/null 2>&1 || true
}

cmd_set_chat_title() {
  local title="$1"
  curl -s --max-time 10 -X POST "${API}/setChatTitle" \
    --data-urlencode "chat_id=${TARGET_CHAT_ID}" \
    --data-urlencode "title=${title}" >/dev/null 2>&1 || true
}

cmd_get_chat_title() {
  local resp title
  resp=$(curl -s --max-time 10 "${API}/getChat?chat_id=${TARGET_CHAT_ID}")
  python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
if d.get('ok'):
    print(d.get('result',{}).get('title',''), end='')
" <<< "$resp" 2>/dev/null || echo ""
}

cmd_is_bot_admin() {
  local me_resp bot_id member_resp
  me_resp=$(curl -s --max-time 10 "${API}/getMe")
  bot_id=$(python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
print(d.get('result', {}).get('id', '') if d.get('ok') else '')
" <<< "$me_resp" 2>/dev/null)
  if [[ -z "$bot_id" ]]; then
    echo "0"
    return
  fi
  member_resp=$(curl -s --max-time 10 "${API}/getChatMember?chat_id=${TARGET_CHAT_ID}&user_id=${bot_id}")
  python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
r = d.get('result', {}) if d.get('ok') else {}
ok = r.get('status') == 'administrator' and bool(r.get('can_change_info'))
print('1' if ok else '0')
" <<< "$member_resp" 2>/dev/null || echo "0"
}

cmd_download_file() {
  local file_id="$1"
  local dest="$2"
  local resp file_path http_code
  resp=$(curl -s --max-time 10 "${API}/getFile?file_id=${file_id}")
  file_path=$(python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
if d.get('ok'):
    print(d['result']['file_path'], end='')
else:
    print('ERR:' + d.get('description','unknown'), file=sys.stderr)
" <<< "$resp")
  if [[ -z "$file_path" ]]; then
    echo "tg.sh: getFile failed for file_id=$file_id resp=${resp:0:200}" >&2
    return 1
  fi
  http_code=$(curl -s --max-time 120 -L -w '%{http_code}' -o "$dest" \
    "${TELEGRAM_API_BASE}/file/bot${TELEGRAM_BOT_TOKEN}/${file_path}")
  if [[ "$http_code" != "200" ]]; then
    echo "tg.sh: download HTTP $http_code for $dest" >&2
    rm -f "$dest"
    return 1
  fi
}

case "${1:-}" in
  send)        shift; cmd_send "$1" ;;
  edit)        shift; cmd_edit "$1" "$2" ;;
  edit_html)   shift; cmd_edit_html "$1" "$2" ;;
  photo)       shift; cmd_photo "$1" "${2:-}" ;;
  document)    shift; cmd_document "$1" "${2:-}" ;;
  media_group) shift; cmd_media_group "$@" ;;
  kb)          shift; cmd_kb "$1" "$2" ;;
  editkb)      shift; cmd_editkb "$1" "$2" "$3" ;;
  setkb)       shift; cmd_setkb "$1" "$2" ;;
  show_reply_kb) shift; cmd_show_reply_kb "$1" "${2:-}" ;;
  request_user_kb) shift; cmd_request_user_kb "$1" "${2:-}" ;;
  answer_cb)   shift; cmd_answer_cb "$1" "${2:-}" ;;
  delete)      shift; cmd_delete "$1" ;;
  react)       shift; cmd_react "$1" "${2:-}" ;;
  download_file)    shift; cmd_download_file "$1" "$2" ;;
  set_chat_title)   shift; cmd_set_chat_title "$1" ;;
  get_chat_title)   shift; cmd_get_chat_title ;;
  is_bot_admin)     shift; cmd_is_bot_admin ;;
  *)
    echo "Usage: $0 {send <text>|edit <msg_id> <text>|photo <path> [caption]|document <path> [caption]|media_group <p1>[::cap1] ...|kb <text> <keyboard_json>|setkb <msg_id> <keyboard_json>|answer_cb <cb_id> [toast]|delete <msg_id>|download_file <file_id> <dest>}" >&2
    exit 1
    ;;
esac
