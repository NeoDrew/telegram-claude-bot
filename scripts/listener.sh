#!/bin/bash
# listener.sh - main daemon. Long-polls Telegram getUpdates, dispatches each
# message. Runs claude spawns in the background so /cancel can interrupt
# mid-run, and queues additional messages while a spawn is busy.
#
# Launched by launchd with KeepAlive=true. Helper scripts (handle_action.sh,
# spawn_claude.sh, tg.sh, ...) are exec'd fresh on each use, so edits to them
# apply immediately. This file itself is parsed once at boot, so its own edits
# need a restart; maybe_auto_restart (below) notices code changes under
# scripts/ and hooks/ and fires a detached restart on its own within seconds.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$REPO_ROOT/scripts"
STATE_DIR="$REPO_ROOT/state"
LOG_DIR="$REPO_ROOT/logs"
ROOMS_DIR="$STATE_DIR/rooms"
mkdir -p "$STATE_DIR" "$LOG_DIR" "$ROOMS_DIR"
LOG="$LOG_DIR/listener.log"
OFFSET_FILE="$STATE_DIR/offset"   # bot-global Telegram getUpdates cursor

# Per-room state lives under state/rooms/<chat_id>/. A "room" is one Telegram
# chat, the user's DM, or a group/topic. Each room has its own session,
# queue, busy-lock, effort, schedule. The allowed sender (TELEGRAM_CHAT_ID
# in .env, semantically the user's user_id) can talk to the bot from any
# room; each room is an independent conversation that can run a claude
# spawn in parallel with other rooms.
room_dir() {
  local chat_id="$1"
  local rd="$ROOMS_DIR/$chat_id"
  mkdir -p "$rd"
  # Lazy-init missing files so per-room helpers don't have to guard.
  # room_state.py owns the sessions.json schema (init only writes if absent).
  [[ -f "$rd/sessions.json" ]] || python3 "$SCRIPTS/room_state.py" init "$rd/sessions.json"
  [[ -f "$rd/queue.jsonl" ]] || : > "$rd/queue.jsonl"
  [[ -f "$rd/effort" ]] || : > "$rd/effort"
  echo "$rd"
}

. "$REPO_ROOT/scripts/lib.sh"   # format_gap() lives here now, shared with spawn_claude.sh

# Idle-room message-burst debounce (see dispatch()'s idle branch and
# drain_queue_if_idle's .debounce_until gate below). How long to wait,
# quiet-period style, after the LAST message from an idle room before
# actually spawning, catches Telegram's own behaviour of splitting one
# long paste into several separate messages sent back-to-back, which
# used to spawn one turn per fragment (claude answering a sentence cut
# off mid-thought, then answering the rest as an unrelated second turn).
# Integer seconds only (bash has no float arithmetic); 2s comfortably
# covers how tightly Telegram's own split-message delivery clusters
# while staying short enough not to read as added latency.
DEBOUNCE_WINDOW_S="$(bot_config BOT_DEBOUNCE_WINDOW_S 2)"
# Context fill (%) at which a new message is preceded by /compact, so the
# user sees: message, compression, answer. A room's own auto compact
# setting wins. Claude Code's own trigger sits near the very top of the
# window and can reject the message instead ("Prompt is too long").
PRECOMPACT_PCT="$(bot_config BOT_PRECOMPACT_PCT 90)"

TELEGRAM_BOT_TOKEN=$(extract_var TELEGRAM_BOT_TOKEN)
TELEGRAM_CHAT_ID=$(extract_var TELEGRAM_CHAT_ID)
# Overridable so the E2E test harness (tests/e2e/) can point the whole
# pipeline at a local fake Bot API server instead of the real Telegram
# backend. Defaults to the real endpoint, so production behaviour is
# unchanged when unset.
TELEGRAM_API_BASE="$(bot_config TELEGRAM_API_BASE https://api.telegram.org)"
export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_API_BASE

if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
  echo "listener.sh: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env" >&2
  exit 2
fi

# Sender allowlist. Trust is on the SENDER's user_id (from.id), never the
# chat_id: the owner (and any extra allowed users) can talk to the bot from
# any chat they share with it, and the reply is routed back by chat_id. The
# owner (TELEGRAM_CHAT_ID) is always allowed. TELEGRAM_ALLOWED_IDS is an
# optional comma/space separated list of additional numeric user_ids. Per the
# DESIGN AXIOM, ids live in .env (config), never hardcoded here. Stored
# space-padded so the gate is a whole-token match (*" $id "*), not a substring.
ALLOWED_SENDERS=" $TELEGRAM_CHAT_ID "
TELEGRAM_ALLOWED_IDS=$(extract_var TELEGRAM_ALLOWED_IDS)
for _aid in ${TELEGRAM_ALLOWED_IDS//,/ }; do
  [[ -n "$_aid" ]] && ALLOWED_SENDERS+="$_aid "
done

# Per-room approved users (state/approved_users.json), managed by the Users
# menu via approved_users.py. Unlike ALLOWED_SENDERS (owner + static extras,
# global, frozen at boot), this is read LIVE: approving a user from the menu
# takes effect on their next message with NO restart. Cached by mtime so the
# hot poll loop only re-flattens the file when it actually changes; the
# flattened form is " <chat_id>:<user_id> ... " for a whole-token match.
APPROVED_FILE="$STATE_DIR/approved_users.json"
APPROVED_MTIME="unset"
APPROVED_PAIRS=" "
refresh_approved() {
  local m
  m=$(stat -f %m "$APPROVED_FILE" 2>/dev/null || echo "")
  [[ "$m" == "$APPROVED_MTIME" ]] && return 0
  APPROVED_MTIME="$m"
  if [[ -s "$APPROVED_FILE" ]]; then
    APPROVED_PAIRS=" $(python3 "$SCRIPTS/approved_users.py" flatten 2>/dev/null | tr '\n' ' ') "
  else
    APPROVED_PAIRS=" "
  fi
}
# Is this sender allowed to drive the bot in this room? The owner and any
# static TELEGRAM_ALLOWED_IDS are allowed in EVERY room; anyone else must be
# approved for THIS specific (chat_id, user_id) pair.
sender_allowed() {  # <chat_id> <user_id>
  [[ "$ALLOWED_SENDERS" == *" $2 "* ]] && return 0
  [[ "$APPROVED_PAIRS" == *" $1:$2 "* ]] && return 0
  return 1
}

log() {
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG"
}

# Bot @username, for the group @-mention gate (in a group the bot only acts on
# messages that @ it). Discovered at runtime via getMe so nothing is hardcoded
# (DESIGN AXIOM), and cached to disk so a transient getMe failure at boot can't
# silence the bot; we fall back to the last known username. Empty only on a cold
# first boot with no network, in which case the gate is disabled (the bot behaves
# as before, acting on every allowlisted message).
BOT_USERNAME_CACHE="$STATE_DIR/.bot_username"
fetch_bot_username() {
  curl -s --max-time 8 "${TELEGRAM_API_BASE}/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null \
    | python3 -c 'import sys,json
try:
    print((json.load(sys.stdin).get("result") or {}).get("username") or "")
except Exception:
    print("")' 2>/dev/null
}
# Retry a few times at boot: a single transient network drop here used to
# leave BOT_USERNAME empty for the whole run, silently disabling the group
# @-mention gate (fail-open) until a manual restart.
BOT_USERNAME=""
for _attempt in 1 2 3; do
  BOT_USERNAME=$(fetch_bot_username)
  [[ -n "$BOT_USERNAME" ]] && break
  sleep 2
done
if [[ -n "$BOT_USERNAME" ]]; then
  echo "$BOT_USERNAME" > "$BOT_USERNAME_CACHE" 2>/dev/null || true
else
  BOT_USERNAME=$(cat "$BOT_USERNAME_CACHE" 2>/dev/null || echo "")
fi
if [[ -n "$BOT_USERNAME" ]]; then
  log "group @-mention gate active for @$BOT_USERNAME"
else
  log "bot username unknown (getMe failed, no cache); group @-mention gate disabled until re-resolve"
fi
# Lazy self-heal: when still unknown, re-resolve in the main loop (rate-
# limited via this timestamp) so the gate comes back without a restart.
BOT_USERNAME_RETRY_AT=0

# ---------------- single-host guard -----------------
# The Telegram getUpdates endpoint is single-consumer per bot token: a second
# listener (e.g. on a second machine with the same plist installed, which
# launchd auto-loads at login) will steal updates from this one and we'll
# silently miss messages. The bot can be pinned to one host; if launchd fires
# us on another host, refuse to start and let launchd retry later.
# Pinned via BOT_PRIMARY_HOST in .env; a .primary_host file still works as a
# fallback for older installs. Unset means no pinning.
PRIMARY_HOST=$(bot_config BOT_PRIMARY_HOST "")
PRIMARY_HOST_FILE="$REPO_ROOT/.primary_host"
if [[ -z "$PRIMARY_HOST" && -f "$PRIMARY_HOST_FILE" ]]; then
  PRIMARY_HOST=$(tr -d '\n\r ' < "$PRIMARY_HOST_FILE")
fi
if [[ -n "$PRIMARY_HOST" ]]; then
  # `hostname -s` reflects the DHCP/mDNS-assigned name, which can drift on a
  # reboot (the router hands out e.g. "host-2.local"), so we also accept the
  # stable, locally-configured LocalHostName. The guard's purpose is to keep a
  # DIFFERENT physical machine from stealing the single-consumer
  # Telegram poll; that machine has a different LocalHostName too, so matching
  # on either name keeps the protection intact while surviving DHCP drift.
  CURRENT_HOST=$(hostname -s)
  CURRENT_LOCALHOST=$(/usr/sbin/scutil --get LocalHostName 2>/dev/null)
  if [[ -n "$PRIMARY_HOST" && "$CURRENT_HOST" != "$PRIMARY_HOST" && "$CURRENT_LOCALHOST" != "$PRIMARY_HOST" ]]; then
    log "single-host guard: current host '$CURRENT_HOST' (LocalHostName '$CURRENT_LOCALHOST') is not primary '$PRIMARY_HOST', refusing to start"
    exit 0
  fi
fi

# ------------------------- self-tests at startup -------------------------
self_test() {
  local errors=0
  if [[ ! -x "$(claude_bin)" ]] && ! command -v claude >/dev/null 2>&1; then
    log "self-test FAIL: claude binary not found"
    errors=$((errors+1))
  fi
  local oauth
  oauth=$(extract_var CLAUDE_CODE_OAUTH_TOKEN)
  if [[ -z "$oauth" ]]; then
    log "self-test FAIL: CLAUDE_CODE_OAUTH_TOKEN missing"
    errors=$((errors+1))
  fi
  local me
  me=$(curl -s --max-time 5 "${TELEGRAM_API_BASE}/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null || true)
  if [[ -n "$me" ]] && [[ "$me" == *'"ok":true'* ]]; then
    log "self-test ok: claude binary, oauth token, telegram reachable"
  else
    log "self-test note: telegram not reachable (transient network drop). Will retry quietly."
  fi
  return $errors
}

if ! self_test; then
  log "self-test failed, exiting (launchd will retry)"
  exit 3
fi

# --------------------- concurrency / busy state helpers ---------------------
# All per-room helpers take the chat_id as the first argument so multiple
# rooms can be busy simultaneously without interference.

# is_busy() now lives in lib.sh, same definition is used by per-command
# handlers (commands/cancel.sh) so the listener's routing and the cancel
# prompt agree on what "busy" means.

start_spawn() {
  local user_text="$1"
  local chat_id="$2"
  # Space-separated Telegram message_ids this spawn is about to answer,
  # threaded through to inject_to_inbox so the streamer can flip their
  # 🫡/😴 "queued" reaction to 👀 once Claude reads it (see
  # inject_to_inbox and stream_to_telegram.py's .pending_reacts handling).
  # Empty for scheduled/internal spawns, which have no Telegram message to
  # react to.
  local msg_ids="${3:-}"
  local rd
  rd=$(room_dir "$chat_id")

  # Diagnostic trail for the "why did this room spawn while it should have
  # been parked / already busy" investigation. start_spawn is
  # the single choke point every caller goes through, so one line here
  # captures caller identity (FUNCNAME[1]: fire_scheduled_tasks /
  # drain_queue_if_idle / dispatch) plus the busy/hold state this call
  # actually observed. Cheap (once per real spawn attempt, not per poll),
  # keep it, the whole reason this bug went unsolved is that nothing
  # recorded this before.
  {
    local _diag_busy_pid _diag_busy_alive="no-lock" _diag_rlu="" _diag_rlu_note="none"
    if [[ -f "$rd/busy.lock" ]]; then
      _diag_busy_pid=$(cat "$rd/busy.lock" 2>/dev/null)
      if [[ -n "$_diag_busy_pid" ]] && kill -0 "$_diag_busy_pid" 2>/dev/null; then
        _diag_busy_alive="ALIVE(pid=$_diag_busy_pid)"
      else
        _diag_busy_alive="STALE(pid=$_diag_busy_pid)"
      fi
    fi
    if [[ -f "$rd/.rate_limited_until" ]]; then
      _diag_rlu=$(cat "$rd/.rate_limited_until" 2>/dev/null)
      if [[ "$_diag_rlu" =~ ^[0-9]+$ ]] && (( $(date +%s) < _diag_rlu )); then
        _diag_rlu_note="$_diag_rlu(FUTURE, should NOT be spawning)"
      else
        _diag_rlu_note="$_diag_rlu(past-or-invalid)"
      fi
    fi
    log "  start_spawn ENTRY chat=$chat_id caller=${FUNCNAME[1]:-unknown} busy.lock=$_diag_busy_alive rate_limited_until=$_diag_rlu_note"
  }

  # Crash-loop circuit breaker: if this room's spawn has CRASHED (not just
  # started) ≥3 times in the last 60s, reset its session and back off.
  #
  # Pre-fix this appended to .crash_times on every spawn start, so 3-4
  # normal user messages within 60s falsely tripped the breaker and reset
  # the session. The append now lives in spawn_claude.sh's crash path
  # (EXIT != 0 and no .user_cancel marker), which means a clean exit,
  # /cancel, and /new no longer count toward the threshold.
  #
  # Count uses awk's NR rather than wc -l + echo so an empty filter
  # result correctly reports 0 (echo "" produces a newline that wc -l
  # counted as 1, the historical off-by-one).
  local crash_file="$rd/.crash_times"
  local now_s
  now_s=$(date +%s)
  if [[ -f "$crash_file" ]]; then
    local crash_count
    crash_count=$(awk -v cutoff=$((now_s - 60)) '$1 > cutoff {n++} END {print n+0}' "$crash_file")
    if [[ "$crash_count" -ge 3 ]]; then
      log "  circuit-breaker: $crash_count crashes in 60s for chat=$chat_id, resetting session and backing off 30s"
      python3 "$SCRIPTS/room_state.py" clear-session "$rd/sessions.json" 2>>"$LOG" || true
      : > "$rd/inbox.ndjson"
      : > "$rd/.pending_reacts"   # keep 1:1 with inbox.ndjson, see inject_to_inbox
      # Also drop the queue: the crash path salvages unconsumed inbox
      # messages into queue.jsonl for retry, so without this the breaker
      # would trip, drain would re-spawn the same poisoned prompt, and the
      # loop would restart. Tripping the breaker means "stop retrying".
      : > "$rd/queue.jsonl"
      : > "$crash_file"
      # Retry the message being sent now, once, in the fresh session.
      # BUG FIXED: the breaker used to drop it and ask the user to resend.
      # A second trip within 10 minutes means the message itself is likely
      # the problem, so that one stops for real.
      local last_trip
      last_trip=$(cat "$rd/.breaker_tripped_at" 2>/dev/null)
      echo "$now_s" > "$rd/.breaker_tripped_at"
      if [[ -z "$user_text" ]] || { [[ "$last_trip" =~ ^[0-9]+$ ]] && (( now_s - last_trip < 600 )); }; then
        CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" send "⚡ Claude keeps crashing, even in a fresh session, so I've stopped retrying. Send your message again when you're ready, or try rephrasing it." >>"$LOG" 2>&1 || true
        return
      fi
      CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" send "⚡ Claude kept crashing, so I've started a fresh session and I'm retrying your message there." >>"$LOG" 2>&1 || true
    fi
    # Prune in place so the file doesn't grow forever.
    local tmp
    tmp="$crash_file.tmp.$$"
    awk -v cutoff=$((now_s - 60)) '$1 > cutoff' "$crash_file" > "$tmp" 2>/dev/null && mv "$tmp" "$crash_file" || rm -f "$tmp"
  fi
  # NOTE: do NOT append "$now_s" here. The append happens in spawn_claude.sh
  # only when a spawn actually crashes.

  # Resolve a friendly label for the per-room viewer window title. Cached
  # in $rd/label so we don't hit getChat on every spawn.
  local label
  label=$(room_label "$chat_id")

  log "  spawning claude (bg) chat=$chat_id label='$label'"
  # Persistent spawns: the LISTENER seeds the inbox before busy.lock makes
  # the room look busy, and tells the spawn the inbox is pre-seeded. The
  # spawn used to truncate-then-seed asynchronously AFTER the lock was
  # written, so a fast follow-up message injected during that warmup window
  # was silently destroyed by the truncate. With listener-side seeding the
  # window does not exist: by the time this function returns, the inbox is
  # seeded and .inbox_ready is up, so the next message injects safely.
  # Silent one-shots don't read the inbox, so they skip the seeding and
  # the ready marker (messages arriving during one run divert to the queue).
  # Before touching the inbox, make sure nothing is still consuming it.
  # Orphaned pipelines (wrapper SIGKILLed, tail+claude reparented to pid 1)
  # are killed wholesale; a LIVE sibling spawn means is_busy raced a stale
  # lock, in that case re-route the message to the sibling instead of
  # truncating its inbox out from under it and double-spawning.
  if ! reap_inbox_pipeline "$chat_id"; then
    log "  start_spawn chat=$chat_id: live sibling spawn detected, re-routing instead of spawning"
    local sib_tail sib_wrapper
    sib_tail=$(pgrep -f "tail -n[^|]* -f $(printf '%s' "$rd/inbox.ndjson" | sed 's/[][\\.*^$()+?{|/]/\\&/g')\$" 2>/dev/null | head -1)
    sib_wrapper=$(ps -o ppid= -p "${sib_tail:-0}" 2>/dev/null | tr -d ' ')
    [[ -n "$sib_wrapper" && "$sib_wrapper" != "1" ]] && echo "$sib_wrapper" > "$rd/busy.lock"
    if [[ -f "$rd/.inbox_ready" ]]; then
      inject_to_inbox "$user_text" "$chat_id" "$msg_ids"
    else
      queue_line "$user_text" "$msg_ids" >> "$rd/queue.jsonl"
    fi
    return
  fi
  local preseeded=0
  if [[ "${CLAUDE_SILENT_SPAWN:-}" != "1" ]]; then
    : > "$rd/inbox.ndjson"
    : > "$rd/.pending_reacts"   # keep 1:1 with inbox.ndjson, see inject_to_inbox
    # If a /compact was requested while the room was busy or idle, inject it
    # BEFORE the user message so the context is compressed first. Coalescing
    # /compact as plain text into the user message (the old queue approach)
    # caused it to be treated as message content rather than a slash command.
    if context_full "$chat_id"; then
      log "  context full chat=$chat_id: compressing before the message"
      touch "$rd/.compact_pending"
    fi
    if [[ -f "$rd/.compact_pending" ]]; then
      rm -f "$rd/.compact_pending"
      inject_to_inbox "/compact" "$chat_id"   # synthetic, no message to react to
      touch "$rd/.compact_in_flight"
    fi
    [[ -n "$user_text" ]] && inject_to_inbox "$user_text" "$chat_id" "$msg_ids"
    touch "$rd/.inbox_ready"
    preseeded=1
  fi
  # Detach the spawn into its own process group via the python setsid
  # trampoline. Without this, the spawn shares the listener's PGID and
  # `launchctl kickstart -k` (which signals the whole group) kills the
  # in-flight claude turn along with the listener. After detach, spawns
  # survive listener restarts and keep streaming to Telegram, the new
  # listener picks back up Telegram polling, the existing spawn's
  # busy.lock keeps the room marked busy until it finishes naturally.
  # Redirect to logs/spawn.log, NOT $LOG (listener.log): spawn_claude.sh's
  # own internal logging already targets spawn.log, and its own alert text
  # tells the user to check spawn.log for details. Piping its raw
  # stdout/stderr into listener.log instead meant any bash-level error
  # from THIS process (a bad substitution, say) landed in the wrong file,
  # invisible to anyone following the alert's own pointer.
  CHAT_ID="$chat_id" BOT_ROOM_LABEL="$label" CLAUDE_INBOX_PRESEEDED="$preseeded" \
    python3 "$SCRIPTS/with_setsid.py" \
      "$SCRIPTS/spawn_claude.sh" "$user_text" >>"$LOG_DIR/spawn.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$rd/busy.lock"
  log "  busy chat=$chat_id pid=$pid (own pgid via setsid)"
}

# Resolve a friendly label for a chat (group title or DM placeholder).
# Cached in state/rooms/<chat>/label.
room_label() {
  local chat_id="$1"
  local rd
  rd=$(room_dir "$chat_id")
  local cache="$rd/label"
  # A user-pinned label (set via /label) is authoritative, never auto-sync
  # over it, even if the Telegram title changes.
  if [[ -f "$rd/.label_pinned" && -s "$cache" ]]; then
    cat "$cache"
    return 0
  fi
  # Otherwise the cache is just a copy of the live Telegram title. Re-fetch if
  # it's missing or older than the TTL so a group RENAME syncs (it used to
  # stick forever: the cache short-circuited and getChat was never called
  # again). One getChat per room per TTL, not per spawn.
  local ttl=3600
  if [[ -s "$cache" ]]; then
    local mtime age
    mtime=$(stat -f %m "$cache" 2>/dev/null || echo 0)
    age=$(( $(date +%s) - mtime ))
    if [[ "$age" -lt "$ttl" ]]; then
      cat "$cache"
      return 0
    fi
  fi
  local resp
  resp=$(curl -s --max-time 6 "${TELEGRAM_API_BASE}/bot${TELEGRAM_BOT_TOKEN}/getChat?chat_id=${chat_id}" 2>/dev/null)
  local label
  label=$(echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if not d.get('ok'):
        print('')
    else:
        r = d.get('result') or {}
        t = r.get('type', '')
        if t in ('group', 'supergroup', 'channel'):
            print(r.get('title') or 'group')
        else:
            # DM. Telegram returns the OTHER end's name, which for the bot
            # owner's DM is the user's own username, confusingly self-named.
            # Default to 'DM' so /rooms shows something obviously useful.
            # The user can override per-room with /label <name>.
            print('DM')
except Exception:
    print('')
" 2>/dev/null)
  if [[ -z "$label" ]]; then
    # getChat failed (network, rate limit). Don't clobber a good cached title
    # with a placeholder, keep showing the last known name if we have one.
    if [[ -s "$cache" ]]; then
      cat "$cache"
      return 0
    fi
    label="room $chat_id"
  fi
  echo "$label" > "$cache"
  echo "$label"
}

# Fire any due scheduled tasks from the GLOBAL state/scheduled.json. Each
# task carries its own target_chat_id, so this is one file scan instead of
# an iterate-all-rooms loop, and tasks can never be misrouted by file
# location (a task's destination never depends on which file or room
# directory it happens to be stored under).
fire_scheduled_tasks() {
  local now sched_file output record chat_id prompt
  now=$(date -u +%s)
  sched_file="$STATE_DIR/scheduled.json"
  [[ -f "$sched_file" ]] || return 0
  output=$(python3 "$SCRIPTS/check_schedule.py" "$sched_file" "$now" 2>>"$LOG")
  [[ -z "$output" ]] && return 0
  # check_schedule.py emits records separated by \x1e (record sep). Each
  # record is "<chat_id>\x1f<prompt>" (\x1f is unit sep). Neither byte
  # appears in user-typed prompts so the split is unambiguous.
  while IFS= read -r -d $'\x1e' record; do
    [[ -z "${record// }" ]] && continue
    chat_id="${record%%$'\x1f'*}"
    prompt="${record#*$'\x1f'}"
    [[ -z "$chat_id" || "$chat_id" == "$record" ]] && {
      log "scheduled task drop: malformed record (no chat_id sep): ${record:0:80}"
      continue
    }
    log "scheduled task firing in chat=$chat_id: ${prompt:0:100}"
    local rd
    rd=$(room_dir "$chat_id")
    local inbox="$rd/inbox.ndjson"
    if is_busy "$chat_id"; then
      if [[ -f "$rd/.inbox_ready" && -f "$inbox" ]]; then
        USER_MSG_TEXT="$prompt" python3 -c "
import json, os
print(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': os.environ['USER_MSG_TEXT']}}))
" >> "$inbox"
        echo '[]' >> "$rd/.pending_reacts"   # keep 1:1 with the inbox, see inject_to_inbox
        log "  injected into active session inbox"
      else
        # Busy with no live inbox consumer (teardown window or a silent
        # one-shot in flight). Queue the prompt so the drain pass delivers
        # it once the room frees up, instead of appending into an inbox
        # nobody reads. Newlines stored as \x1e (queue is line-oriented).
        printf '%s\n' "${prompt//$'\n'/$'\x1e'}" >> "$rd/queue.jsonl"
        log "  queued scheduled prompt (busy, inbox not ready)"
      fi
    elif is_rate_limited "$rd/.rate_limited_until"; then
      # Room parked on a 429 hold: a silent spawn now would crash on the
      # same limit with the task already consumed. Queue for auto-resume.
      printf '%s\n' "${prompt//$'\n'/$'\x1e'}" >> "$rd/queue.jsonl"
      log "  queued scheduled prompt (rate-limit hold)"
    else
      CLAUDE_SILENT_SPAWN=1 start_spawn "$prompt" "$chat_id"
    fi
  done < <(printf '%s\x1e' "$output")
}

drain_queue_if_idle() {
  local rd chat_id qf draining combined count
  for rd in "$ROOMS_DIR"/*/; do
    [[ -d "$rd" ]] || continue
    rd="${rd%/}"
    chat_id="${rd##*/}"
    if is_busy "$chat_id"; then continue; fi
    # Idle-burst debounce: dispatch()'s idle branch buffers the first
    # message from an idle room here instead of spawning immediately, and
    # stamps this marker with "now + DEBOUNCE_WINDOW_S", refreshed on
    # every additional message that arrives before it expires, so a burst
    # (Telegram's own split-long-paste fragments, or several quick
    # separate messages) all lands in ONE coalesced turn. Only touched by
    # that branch: the rate-limit-hold and busy-not-ready queue paths
    # below never set it, so their queued content still drains the moment
    # the room frees, unaffected by this.
    if [[ -f "$rd/.debounce_until" ]]; then
      local _db_until
      _db_until=$(cat "$rd/.debounce_until" 2>/dev/null)
      if [[ "$_db_until" =~ ^[0-9]+$ ]] && (( $(date +%s) < _db_until )); then
        continue   # still within the quiet period, wait for more messages
      fi
      rm -f "$rd/.debounce_until"
    fi
    # Rate-limit hold: a spawn that died on a 429 parks the room until the
    # limit's reset epoch (.rate_limited_until, written by spawn_claude.sh
    # with the session PRESERVED). Re-spawning before then just burns a
    # crash on the same 429. Once the hold expires, drop the marker and
    # drain normally, that respawn IS the auto-continue.
    if [[ -f "$rd/.rate_limited_until" ]]; then
      if is_rate_limited "$rd/.rate_limited_until"; then
        continue
      fi
      local _diag_rlu_seen _diag_now_seen
      _diag_rlu_seen=$(cat "$rd/.rate_limited_until" 2>/dev/null)
      _diag_now_seen=$(date +%s)
      rm -f "$rd/.rate_limited_until"
      log "  rate-limit hold expired chat=$chat_id, resuming (stored_until='$_diag_rlu_seen' now=$_diag_now_seen)"
      emit_metric rate_limit_resume room="$chat_id"
      qf="$rd/queue.jsonl"
      if [[ ! -s "$qf" ]]; then
        # Limit cleared but no queued messages to auto-resume. Notify so the
        # user knows the room is ready for their next message.
        CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" send "✅ Rate limit cleared, ready to continue." >>"$LOG" 2>&1 || true
        continue
      fi
    fi
    qf="$rd/queue.jsonl"
    if [[ ! -s "$qf" && -f "$rd/.compact_pending" ]] \
        && [[ -z "$(python3 "$SCRIPTS/room_state.py" get-session "$rd/sessions.json" 2>/dev/null)" ]]; then
      rm -f "$rd/.compact_pending" "$rd/.compact_in_flight"   # no conversation to compress
    fi
    if [[ ! -s "$qf" && -f "$rd/.compact_pending" ]]; then
      # 🗜 tapped in an idle chat: start Claude just to compress, now,
      # rather than making the next message wait through it. start_spawn
      # injects the /compact first and, with no message, nothing after.
      log "  compact-only spawn chat=$chat_id"
      start_spawn "" "$chat_id"
      continue
    fi
    [[ -s "$qf" ]] || continue
    # Atomic claim: rename the queue out from under any concurrent dispatch
    # before reading. A `dispatch` running on the next iteration that
    # appends to $qf will land in a NEW empty queue.jsonl, not in the file
    # we're draining. Pre-fix this used read-then-truncate which raced.
    draining="$qf.draining.$$"
    if ! mv "$qf" "$draining" 2>/dev/null; then
      continue
    fi
    : > "$qf"
    # Coalesce one message per physical line with blank-line separators,
    # then restore intra-message newlines (\x1e was the storage escape).
    # Bash parameter expansion, NOT `tr '\x1e' '\n'`: BSD tr (macOS) has no \xHH hex-escape support, so it silently parsed SET1 as
    # the four LITERAL characters \, x, 1, e and mapped every occurrence
    # of ANY of them, including the plain letter 'e', to a newline. Any
    # queued message that went through this coalesce path had every 'e'
    # eaten and replaced with a line break (queued text arrived chopped up
    # with letters missing). ${VAR//pat/repl} is what other call sites in
    # this file already use safely for the identical substitution; this call
    # site was the one inconsistent spot still piping through external tr.
    read_queue "$draining"
    count=$(grep -c . "$draining" 2>/dev/null || echo 0)
    rm -f "$draining"
    [[ -z "$QTEXT" ]] && continue
    log "  dequeue chat=$chat_id: ${count} message(s) coalesced"
    emit_metric dequeue room="$chat_id" count="$count"
    start_spawn "$QTEXT" "$chat_id" "$QIDS"
  done
}

# drain_inject_burst() - paired with drain_queue_if_idle above, but for
# the busy+ready inject path's own debounce (dispatch()'s "idle between
# turns" branch, .inject_burst.jsonl / .inject_debounce_until). Flushes a
# settled burst as ONE inject_to_inbox call so a Telegram-split long paste
# lands as a single claude turn instead of being answered after only its
# first fragment (see the buffering branch's own comment for the full
# story). Separate file/marker pair from queue.jsonl/.debounce_until
# deliberately: a room can simultaneously be mid-flush of an old
# busy-not-ready queue AND collecting a fresh inject burst, and the two
# drain on different conditions (queue drains once NOT busy; this drains
# once busy+ready AND the settle window has passed), conflating them
# would race.
drain_inject_burst() {
  local rd chat_id bf draining combined count until
  for rd in "$ROOMS_DIR"/*/; do
    [[ -d "$rd" ]] || continue
    rd="${rd%/}"
    chat_id="${rd##*/}"
    [[ -f "$rd/.inject_debounce_until" ]] || continue
    until=$(cat "$rd/.inject_debounce_until" 2>/dev/null)
    if [[ "$until" =~ ^[0-9]+$ ]] && (( $(date +%s) < until )); then
      continue   # still within the quiet period, wait for more fragments
    fi
    rm -f "$rd/.inject_debounce_until"
    bf="$rd/.inject_burst.jsonl"
    [[ -s "$bf" ]] || continue
    draining="$bf.draining.$$"
    if ! mv "$bf" "$draining" 2>/dev/null; then
      continue
    fi
    : > "$bf"
    read_queue "$draining"
    combined="$QTEXT"
    count=$(grep -c . "$draining" 2>/dev/null || echo 0)
    rm -f "$draining"
    [[ -z "$combined" ]] && continue
    if is_busy "$chat_id" && [[ -f "$rd/.inbox_ready" ]]; then
      log "  inject-debounce flush chat=$chat_id: ${count} message(s) coalesced into inbox"
      emit_metric dequeue room="$chat_id" count="$count"
      if context_full "$chat_id"; then
        log "  context full chat=$chat_id: compressing before the message"
        inject_to_inbox "/compact" "$chat_id"
      fi
      inject_to_inbox "$combined" "$chat_id" "$QIDS"
    else
      # The spawn this burst was waiting on died or tore down mid-window
      # (a /cancel, a crash, a rate-limit hold), rare, but the content
      # must not be silently dropped. Fall back to the ordinary queue: the
      # room's normal idle-drain / busy-not-ready machinery will pick it
      # up and deliver it as a fresh turn once the room is actually ready
      # for one, same "never silently lost" guarantee every other queue
      # path in this file already gives.
      queue_line "$combined" "$QIDS" >> "$rd/queue.jsonl"
      log "  inject-debounce fallback chat=$chat_id: room state changed, requeued ${count} message(s)"
    fi
  done
}

# Missed-wakeup rescue. Claude Code fires a ScheduleWakeup only while the
# session has no background task running, so a single never-ending
# background wait silently starves a /loop (e.g. a stuck `until grep` wait
# can swallow every wakeup for hours).
# stream_to_telegram.py records each scheduled wakeup in .wakeup_due and
# clears it when the loop stops or the assistant starts a turn after the due time.
# If one is still pending WAKEUP_GRACE_S past due while the room sits idle
# between turns, deliver its prompt ourselves, once. A spawn that died
# takes its in-memory wakeup with it, so the marker is dropped then too.
WAKEUP_GRACE_S="$(bot_config BOT_WAKEUP_GRACE_S 120)"
WAKEUP_CHECK_INTERVAL_S="$(bot_config BOT_WAKEUP_CHECK_INTERVAL_S 30)"
_last_wakeup_check_at=0
rescue_missed_wakeups() {
  local now rd chat_id info due prompt state
  now=$(date +%s)
  (( now - _last_wakeup_check_at < ${WAKEUP_CHECK_INTERVAL_S:-30} )) && return 0
  _last_wakeup_check_at=$now
  for rd in "$ROOMS_DIR"/*/; do
    rd="${rd%/}"
    [[ -f "$rd/.wakeup_due" ]] || continue
    chat_id="${rd##*/}"
    if ! is_busy "$chat_id"; then
      rm -f "$rd/.wakeup_due"
      continue
    fi
    [[ -f "$rd/.inbox_ready" ]] || continue
    info=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(int(d.get('due', 0)))
print(d.get('prompt', ''))
" "$rd/.wakeup_due" 2>/dev/null) || { rm -f "$rd/.wakeup_due"; continue; }
    due="${info%%$'\n'*}"
    prompt="${info#*$'\n'}"
    [[ "$due" =~ ^[0-9]+$ ]] || { rm -f "$rd/.wakeup_due"; continue; }
    (( now < due + WAKEUP_GRACE_S )) && continue
    state=$(REPO_ROOT="$REPO_ROOT" ROOM_DIR="$rd" python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from pathlib import Path
from spawn_state import classify
print(classify(Path(os.environ['ROOM_DIR'])).get('state', ''))
" 2>/dev/null)
    [[ "$state" == "idle" ]] || continue
    rm -f "$rd/.wakeup_due"
    # A slash prompt (e.g. "/loop ...") is delivered verbatim so it runs
    # exactly as the real wakeup would have; anything else (including the
    # <<autonomous-loop-dynamic>> sentinel, which only the harness can
    # resolve) gets a short explanation.
    if [[ "$prompt" != /* ]]; then
      prompt="[Scheduled wakeup] Your ScheduleWakeup was due $(( (now - due) / 60 )) min ago but Claude Code did not fire it, most likely because a background task is still running (wakeups never fire while one is). Check for stuck background tasks, then carry on with the loop. Wakeup prompt: ${prompt}"
    fi
    log "  wakeup rescue chat=$chat_id: due $(( now - due ))s ago, room idle, injecting the wakeup prompt"
    emit_metric wakeup_rescued room="$chat_id" late_s="$(( now - due ))"
    inject_to_inbox "$prompt" "$chat_id"
  done
}

# handle_cancel_inline() removed, the confirm prompt and kill flow now
# live in scripts/commands/cancel.sh, called via handle_action.sh "cancel"
# (no args = prompt) or handle_action.sh "cancel" "yes" (confirm = kill).

# Append a user message NDJSON line to a room's persistent inbox so the
# tail -f → claude pipeline picks it up at the next inter-turn boundary.
#
# msg_ids (optional, 3rd arg): space-separated Telegram message_ids this
# inbox line represents. "Claude consumes one line per turn" (see
# CLAUDE.md), so every call here corresponds to exactly one future turn,
# and we push a matching line to .pending_reacts (JSON array, empty when
# msg_ids is blank e.g. the synthetic "/compact" inject) so the two files
# always stay 1:1. stream_to_telegram.py reads .pending_reacts by
# index as Claude reads each inbox line and flips those messages' 🫡/😴 "queued"
# reaction to 👀, closing the "emoji never updates once Claude actually
# saw it" gap. Kept 1:1-by-construction
# deliberately: every caller that truncates inbox.ndjson (crash salvage,
# circuit breaker, kill_room_spawn, the self-seed branch) ALSO truncates
# .pending_reacts in the same breath, so a crash mid-turn can't leave a
# stale entry that misattributes a later turn's reaction to the wrong
# message, it just degrades to "reaction never updates" for that one
# message, exactly today's behaviour, never a wrong-message update.
# queue_line <text> [msg_ids]: one line for queue.jsonl or .inject_burst.jsonl.
# Newlines are stored as \x1e; the Telegram message ids the text carries
# follow a \x1f (which Telegram text never contains: the parser splits
# updates on it), so the drain can hand them on and the streamer can move
# those messages to 👀 when Claude reads them.
# BUG FIXED: queued messages (idle-chat debounce, Claude starting or
# stopping, a rate-limit hold) used to lose their ids, so their reaction
# never changed and the first message to an idle chat got none at all.
queue_line() {
  local t="${1//$'\n'/$'\x1e'}"
  if [[ -n "${2:-}" ]]; then
    printf '%s\x1f%s\n' "$t" "$2"
  else
    printf '%s\n' "$t"
  fi
}

# read_queue <file>: QTEXT = the lines coalesced with blank-line separators
# (newlines restored), QIDS = every message id they carry, space separated.
read_queue() {
  QIDS=$(awk -F$'\x1f' 'NF > 1 { printf "%s ", $2 }' "$1")
  QIDS="${QIDS% }"
  QTEXT=$(awk -F$'\x1f' '$1 != "" { print $1; print "" }' "$1" | sed -e '$d')
  QTEXT="${QTEXT//$'\x1e'/$'\n'}"
}

# context_full <chat_id>: true when the room's last turn left the context at
# or above its compaction threshold (the room's autocompact setting, else
# PRECOMPACT_PCT). Consumes the reading, so one burst compacts once. Only
# called where Claude is between turns: mid-turn, Claude Code compacts by
# itself between tool calls.
context_full() {
  local rd="$ROOMS_DIR/$1" pct limit
  [[ -f "$rd/.ctx_pct" ]] || return 1
  pct=$(tr -dc '0-9' < "$rd/.ctx_pct")
  limit=$(tr -dc '0-9' < "$rd/autocompact" 2>/dev/null)
  [[ -n "$limit" ]] || limit="$PRECOMPACT_PCT"
  [[ -n "$pct" ]] && (( pct >= limit )) || return 1
  rm -f "$rd/.ctx_pct"
  return 0
}

inject_to_inbox() {
  local text="$1"
  local chat_id="$2"
  local msg_ids="${3:-}"
  local rd="$ROOMS_DIR/$chat_id"
  local inbox="$rd/inbox.ndjson"
  TEXT="$text" python3 <<'PYEOF' >> "$inbox"
import json, os
print(json.dumps({"type": "user",
                  "message": {"role": "user",
                              "content": os.environ['TEXT']}}))
PYEOF
  MSG_IDS="$msg_ids" python3 -c "
import json, os
ids = [int(x) for x in os.environ.get('MSG_IDS', '').split() if x.isdigit()]
print(json.dumps(ids))
" >> "$rd/.pending_reacts"
}

# invoke_action <action_text> <chat_id>
#
# Run handle_action.sh with the given "<cmd> [args]" string and post its reply
# (if any) with a back-to-menu keyboard auto-attached. Used by both the
# callback path (which arrives as "act:<action_text>" and has CALLBACK_MSG_ID
# set so we edit the source message in place) and the typed-/menu path.
invoke_action() {
  local action_text="$1"
  local chat_id="$2"
  local cmd_token
  cmd_token=$(printf '%s' "$action_text" | awk '{print tolower($1)}')
  log "  action chat=$chat_id cmd='$cmd_token'"
  local reply cmd_rc back_kb sent=0
  reply=$("$SCRIPTS/handle_action.sh" "$action_text" 2>>"$LOG")
  cmd_rc=$?
  if [[ -z "$reply" ]]; then
    # No stdout: the handler drew its own keyboard (via reply_kb) or chose
    # to stay silent. Still record that it ran.
    log "  action '$cmd_token' done rc=$cmd_rc (own keyboard / no text reply)"
    return
  fi
  # Every text-only reply gets a back-to-menu row auto-attached so every
  # flow is one tap from the main palette.
  back_kb='[[{"text":"⬅ Menu","callback_data":"act:menu"}]]'
  if [[ -n "${CALLBACK_MSG_ID:-}" ]]; then
    # editMessageText can fail (message >48h old or already gone). Fall
    # back to a fresh kb-send so the user still sees the reply.
    if "$SCRIPTS/tg.sh" editkb "$CALLBACK_MSG_ID" "$reply" "$back_kb" >>"$LOG" 2>&1 \
       || "$SCRIPTS/tg.sh" kb "$reply" "$back_kb" >>"$LOG" 2>&1; then
      sent=1
    else
      log "  tg editkb+kb fallback failed"
    fi
  else
    if "$SCRIPTS/tg.sh" kb "$reply" "$back_kb" >>"$LOG" 2>&1; then
      sent=1
    else
      log "  tg kb send failed for action reply"
    fi
  fi
  log "  action '$cmd_token' done rc=$cmd_rc reply_sent=$sent (${#reply}B)"
}

dispatch() {
  local text="$1"
  local chat_id="$2"
  local sender_name="${3:-}"

  # Export so handle_action.sh, tg.sh, spawn_claude.sh, and any subprocess
  # they spawn (notify-cmd from inside claude) know which room they belong
  # to. Falls back to the .env TELEGRAM_CHAT_ID if anything reads CHAT_ID
  # without it being set.
  export CHAT_ID="$chat_id"
  export CALLBACK_MSG_ID="${CALLBACK_MSG_ID:-}"

  local back_kb='[[{"text":"⬅ Menu","callback_data":"act:menu"}]]'

  # ── Inline-keyboard action ────────────────────────────────────────────
  # Wire format: callback_data = "act:<cmd>[ <args>]". CALLBACK_MSG_ID must
  # be set (only real button taps reach here with that envelope); a typed
  # "act:..." string from a user is rejected by the slash-or-text branches
  # below so the prefix can't be abused to escape menu-only enforcement.
  if [[ "${text:0:4}" == "act:" ]] && [[ -n "${CALLBACK_MSG_ID:-}" ]]; then
    # SLOW READ-ONLY handlers run backgrounded: rooms/users fan getChat out
    # over every room and windows/tab do Quartz captures, and inline they
    # froze message handling for EVERY room until done. State-MUTATING
    # verbs (cancel/cd/new/forget tear down spawns; restart reloads the
    # bot) must stay serial: backgrounding them races the teardown against
    # the very next message's dispatch and loses it.
    local act_verb
    act_verb=$(printf '%s' "${text#act:}" | awk '{print tolower($1)}')
    case "$act_verb" in
      rooms|users|usage|status|windows|tab|help)
        invoke_action "${text#act:}" "$chat_id" &
        ;;
      *)
        invoke_action "${text#act:}" "$chat_id"
        ;;
    esac
    return
  fi

  # ── Awaiting-label intercept ──────────────────────────────────────────
  # The label handler arms `.awaiting_label`; the very next plain text
  # message in this room becomes the new label. Runs BEFORE the slash
  # gate so the user tapping a menu button mid-label-prompt clears the
  # marker (the next branch sees the slash and falls through to the
  # action path) without stealing the typed slash as a label.
  local rd
  rd=$(room_dir "$chat_id")
  if [[ -f "$rd/.awaiting_label" ]]; then
    rm -f "$rd/.awaiting_label"
    if [[ "${text:0:1}" != "/" ]] && [[ "${text:0:4}" != "act:" ]]; then
      log "  consumed as label: '${text:0:60}'"
      local reply
      reply=$("$SCRIPTS/handle_action.sh" "label" "$text" 2>>"$LOG")
      if [[ -n "$reply" ]]; then
        "$SCRIPTS/tg.sh" kb "$reply" "$back_kb" >>"$LOG" 2>&1 || true
      fi
      return
    fi
    # User aborted by tapping a menu button, fall through to the action
    # dispatch below.
  fi

  # ── Awaiting-OAuth-code intercept ─────────────────────────────────────
  # /login arms state/.awaiting_oauth_code; the next plain message is the
  # code Anthropic showed on the redirect page. Bot-global (not per-room)
  # because `claude setup-token` is a per-Mac-user process.
  if [[ -f "$REPO_ROOT/state/.awaiting_oauth_code" ]]; then
    if [[ "${text:0:1}" != "/" ]] && [[ "${text:0:4}" != "act:" ]]; then
      local code lockfile
      code=$(printf '%s' "$text" | tr -d '[:space:]')
      lockfile="$REPO_ROOT/state/.login_in_progress"
      if [[ -f "$lockfile" ]] && kill -0 "$(cat "$lockfile" 2>/dev/null)" 2>/dev/null; then
        log "  forwarding oauth code to watcher (len=${#code})"
        local tmp="$REPO_ROOT/state/.oauth_code.tmp.$$"
        printf '%s\n' "$code" > "$tmp"
        mv -f "$tmp" "$REPO_ROOT/state/.oauth_code"
        rm -f "$REPO_ROOT/state/.awaiting_oauth_code"
        return
      else
        log "  oauth code received but watcher dead"
        rm -f "$REPO_ROOT/state/.awaiting_oauth_code"
        "$SCRIPTS/tg.sh" kb "❌ Login watcher isn't running anymore. Tap *🔐 login* on the menu to restart the flow." "$back_kb" >>"$LOG" 2>&1 || true
        return
      fi
    fi
    # User cancelled the flow by tapping a button, clear the marker and
    # let the action branch render the destination they chose.
    rm -f "$REPO_ROOT/state/.awaiting_oauth_code"
  fi

  # ── Typed slash gate ──────────────────────────────────────────────────
  # /menu and /start are this BOT's only two typeable menu commands (so a
  # freshly-added bot can be opened from a cold chat); both route to the
  # "menu" action. Everything else starting with "/" is NOT rejected: it
  # falls through to the normal plain-text dispatch below and is sent to
  # Claude as ordinary message content, same as if it had no leading
  # slash. This used to reject every other typed slash outright ("Slash
  # commands are menu-driven"), which also blocked Claude Code's OWN
  # slash commands and skills (e.g. "/compact", "/loop", "/goal") from
  # ever reaching Claude as message text, since there was no way to type
  # one into this chat at all. This bot's own commands were never reachable by
  # typing anyway (see "Menu-first UX" above), so narrowing the gate to
  # just /menu and /start loses no bot functionality.
  if [[ "${text:0:1}" == "/" ]]; then
    local first_word
    first_word=$(echo "$text" | awk '{print $1}')
    # Strip @botname suffix that Telegram appends in groups.
    first_word="${first_word%%@*}"
    local typed_cmd_lc
    typed_cmd_lc=$(echo "$first_word" | awk '{print tolower($0)}')
    if [[ "$typed_cmd_lc" == "/menu" || "$typed_cmd_lc" == "/start" ]]; then
      invoke_action "menu" "$chat_id"
      return
    fi
    log "  typed slash '$typed_cmd_lc' is not a bot menu command, forwarding to claude as text"
  fi

  # ── Typed "act:" probe ───────────────────────────────────────────────
  # Reaching here means text starts with "act:" but CALLBACK_MSG_ID is
  # empty, someone typed it in chat trying to bypass the slash gate.
  # Same response as any other typed slash: send them back to the menu.
  if [[ "${text:0:4}" == "act:" ]]; then
    log "  reject typed action probe: '${text:0:60}'"
    "$SCRIPTS/tg.sh" kb \
      "Slash commands are menu-driven, tap below to open the menu." \
      "$back_kb" >/dev/null 2>&1 || true
    return
  fi

  # ── Menu shortcuts ───────────────────────────────────────────────────
  # Typed "m" or "menu" (any case, whitespace ignored) opens the menu.
  # The persistent reply keyboard fires "📋 Menu" as its button text, so
  # match that exact label too. Works identically in DMs and group chats
  # because allowlisting happens before dispatch, only the bot owner's
  # text ever reaches this branch, so we don't accidentally hijack a
  # group member's literal "m" message (they wouldn't get this far).
  # Strip the bot's @mention before matching so "@botname m" in a group
  # (typed to get past the @-mentions-only gate) still hits the shortcut
  # instead of falling through to a full claude spawn.
  local shortcut
  shortcut="$text"
  if [[ -n "${BOT_USERNAME:-}" ]]; then
    shortcut="${shortcut//@$BOT_USERNAME/}"
  fi
  shortcut=$(printf '%s' "$shortcut" | awk '{print tolower($0)}' \
             | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  if [[ "$shortcut" == "m" || "$shortcut" == "menu" \
        || "$shortcut" == "📋 menu" ]]; then
    log "  menu shortcut: '${text:0:30}'"
    invoke_action "menu" "$chat_id"
    return
  fi

  # ── Plain text → user message to claude ──────────────────────────────
  # Group @-mention gate, OPT-IN per room via the menu (🔔 @-mentions only).
  # ── AskUser answer intercept ─────────────────────────────────────────
  # Checked BEFORE the @-mentions gate: a pending question must accept a
  # free-text reply even when mentions_only is on. Button-based answers go
  # through ask_answer.sh (via the act: branch above) instead.
  if [[ -f "$ROOMS_DIR/$chat_id/.ask_user_pending.json" ]]; then
    PENDING_JSON="$ROOMS_DIR/$chat_id/.ask_user_pending.json"
    printf '%s' "$text" > "$ROOMS_DIR/$chat_id/.ask_user_answer"
    log "  ask_user free-text answer written for chat=$chat_id"
    # Edit the question message to show the answer and clear the keyboard,
    # matching the UX of button-tap answers from ask_answer.sh.
    ASK_MSG_ID=$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get('msg_id', 0))
except Exception:
    print(0)
" "$PENDING_JSON" 2>/dev/null || echo 0)
    if [[ "$ASK_MSG_ID" != "0" && "$ASK_MSG_ID" != "" ]]; then
      SAFE_ANS=$(printf '%s' "$text" | python3 -c \
        "import sys,html; print(html.escape(sys.stdin.read().strip()))" 2>/dev/null \
        || printf '%s' "$text")
      SAFE_PROMPT=$(python3 -c "
import json, sys, html
try:
    print(html.escape(json.load(open(sys.argv[1])).get('prompt','')[:150]))
except Exception:
    print('')
" "$PENDING_JSON" 2>/dev/null || echo "")
      EDIT_TXT="<b>$SAFE_ANS</b>"
      [[ -n "$SAFE_PROMPT" ]] && EDIT_TXT+="
<i>$SAFE_PROMPT</i>"
      CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" edit_html "$ASK_MSG_ID" "$EDIT_TXT" >/dev/null 2>&1 || true
    fi
    if [[ -n "${USER_MSG_ID:-}" ]]; then
      CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" react "$USER_MSG_ID" "👀" 2>/dev/null || true
    fi
    return 0
  fi

  # When that room's setting is ON, in a group the bot acts only on messages that
  # @-mention it; ordinary chatter is ignored. Default off → behaves as before.
  # DMs always ignore the setting (the whole chat is addressed to the bot). Menu /
  # slash / "m" shortcuts are handled earlier and are unaffected. The setting
  # is read live from the room dir. Disabled if the bot username is unknown
  # (getMe failed, no cache) so a lookup failure can't silence the bot.
  if [[ "$chat_id" == -* && -n "${BOT_USERNAME:-}" ]]; then
    local _mf="$ROOMS_DIR/$chat_id/mentions_only"
    if [[ -f "$_mf" && "$(tr -d ' \n\r' < "$_mf")" == "on" ]] \
       && ! mentions_bot "$text" "$BOT_USERNAME"; then
      log "  ignore (group, @-only on, not @-mentioned): '${text:0:50}'"
      return
    fi
  fi
  # Strip the @mention so claude sees the request, not the handle. Best-effort,
  # exact-case (Telegram's mention autocomplete uses the canonical username).
  if [[ -n "${BOT_USERNAME:-}" ]]; then
    text="${text//@$BOT_USERNAME/}"
    text="${text//  / }"; text="${text# }"
  fi

  # In a group several people share one conversation, so prefix the sender's
  # name (the spawn's system prompt explains the "[Name]:" convention) so the
  # assistant knows who is speaking.
  #
  # Time-gap note: a persistent claude session has no wall-clock sense
  # between turns, it just sees the next inbox line, whether that arrived
  # 30 seconds or 6 hours later. Without a signal, "what's our status"
  # sent hours after the last message reads as an immediate follow-up and
  # gets answered like one (repeating stale state instead of re-checking
  # it). Stamp a "+Xh Ym later" note on any message that arrives at least
  # GAP_THRESHOLD_S after the room's last one, so claude can tell continuous
  # chatter from a cold re-open. The system prompt explains what this
  # annotation means; it is never part of the person's actual text.
  local time_note="" gap_s
  local last_ts_file="$rd/.last_msg_ts"
  if [[ -s "$last_ts_file" ]]; then
    local last_ts
    last_ts=$(cat "$last_ts_file" 2>/dev/null)
    if [[ "$last_ts" =~ ^[0-9]+$ ]]; then
      gap_s=$(( $(date +%s) - last_ts ))
      local GAP_THRESHOLD_S=1200   # 20 minutes
      if (( gap_s >= GAP_THRESHOLD_S )); then
        time_note="+$(format_gap "$gap_s") later"
      fi
    fi
  fi
  date +%s > "$last_ts_file"

  # Every message carries a "Telegram" tag in the same bracket as the name
  # and time-gap fields, on EVERY message including a bare DM, not just
  # the cases that already had a bracket for some other reason. A one-time
  # system-prompt mention ("you're being spoken to through a Telegram chat,
  # not a terminal") kept fading deep into a long persistent conversation,
  # the same failure mode the name/time-gap annotations were built to avoid
  # for THEIR information: something restated in the actual turn content,
  # every turn, survives compaction and long context far better than a
  # single instruction stated once at the start.
  local bracket="Telegram"
  if [[ "$chat_id" == -* && -n "$sender_name" ]]; then
    bracket="$sender_name, Telegram"
  fi
  if [[ -n "$time_note" ]]; then
    bracket="$bracket, $time_note"
  fi
  local outgoing="[$bracket]: $text"

  # Stale-spawn check: a room that's busy AND ready for injection (idle
  # between turns, not mid-generation) might be running code that predates
  # an on-disk script change, a persistent spawn reads its script text
  # once at exec time, so an edit made after that is invisible to it until
  # it dies and restarts, which normally only happens via a crash, rate
  # limit, or manual /cancel. Refresh it BEFORE routing this message, so
  # the reply comes from current code instead of silently running whatever
  # bugs existed when that process started (a spawn can otherwise survive
  # indefinitely, well past several since-fixed bugs, with nothing short of
  # that room happening to crash or the user manually cancelling ever
  # forcing a refresh). Scoped to the "ready" case only, a spawn
  # mid-teardown or a silent one-shot task is left alone; killing those
  # wouldn't help and could interfere with a task already in progress.
  # kill_room_spawn leaves busy.lock removed, so is_busy() below correctly
  # reports false afterward and this message routes through the normal
  # idle path, no other change needed.
  if is_busy "$chat_id" && [[ -f "$rd/.inbox_ready" ]]; then
    local _spawn_started _newest_code
    _spawn_started=$(cat "$rd/.spawn_started_at" 2>/dev/null)
    if [[ ! "$_spawn_started" =~ ^[0-9]+$ ]]; then
      # .spawn_started_at is only written by spawn_claude.sh code that was
      # added at the same time as this whole check, so EVERY spawn that
      # was already running before this feature existed has no baseline
      # and silently fell straight through the check above, forever (the
      # log line below would never fire for them: the
      # rooms most in need of a staleness check, the old ones, were
      # exactly the ones it couldn't see). Fall back to busy.lock's own
      # mtime, written at the same moment the spawn actually launched, as
      # the best available approximation for a pre-existing spawn.
      _spawn_started=$(stat -f %m "$rd/busy.lock" 2>/dev/null)
    fi
    if [[ "$_spawn_started" =~ ^[0-9]+$ ]]; then
      _newest_code=$(code_mtime)
      # A Claude Code update counts as a code change too (see claude_bin_mtime).
      local _bin_m
      _bin_m=$(claude_bin_mtime)
      if [[ "$_bin_m" =~ ^[0-9]+$ ]] && { [[ ! "$_newest_code" =~ ^[0-9]+$ ]] || (( _bin_m > _newest_code )); }; then
        _newest_code=$_bin_m
      fi
      # Settings read once at spawn start count too: the room's auto
      # compact threshold, model and effort, and a plugin update
      # (commands/plugin.sh touches state/.plugins_updated). Those handlers
      # no longer stop a running spawn themselves; this applies them at
      # the next message instead.
      local _f _m
      for _f in "$rd/autocompact" "$rd/model" "$rd/effort" "$STATE_DIR/.plugins_updated"; do
        _m=$(stat -f %m "$_f" 2>/dev/null)
        if [[ "$_m" =~ ^[0-9]+$ ]] && { [[ ! "$_newest_code" =~ ^[0-9]+$ ]] || (( _m > _newest_code )); }; then
          _newest_code=$_m
        fi
      done
      if [[ "$_newest_code" =~ ^[0-9]+$ ]] && (( _newest_code > _spawn_started )); then
        # Only between turns. BUG FIXED: this used to kill a spawn that was
        # mid-reply, so the reply stopped mid-sentence and follow-ups
        # already queued in its inbox were thrown away. A busy spawn keeps
        # its old code for this message; a later one, sent between turns,
        # refreshes it.
        local _st
        _st=$(REPO_ROOT="$REPO_ROOT" ROOM_DIR="$rd" python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from pathlib import Path
from spawn_state import classify
print(classify(Path(os.environ['ROOM_DIR'])).get('state', ''))
" 2>/dev/null)
        if [[ "$_st" == "idle" ]]; then
          log "  stale spawn chat=$chat_id: started=$_spawn_started code_mtime=$_newest_code, refreshing before dispatch"
          kill_room_spawn "$rd" stale
        else
          log "  stale spawn chat=$chat_id: state=${_st:-unknown}, mid-turn, refresh deferred"
        fi
      fi
    fi
  fi

  local react_emoji
  if is_busy "$chat_id"; then
    if [[ -f "$ROOMS_DIR/$chat_id/.inbox_ready" ]]; then
      # Persistent claude reads from inbox.ndjson via `tail -f`. Append the
      # new message and let claude consume it at the next inter-turn
      # boundary, no queueing, no separate spawn. This mirrors interactive
      # Claude Code's "type while busy; lands at next gap" behaviour.
      #
      # spawn_state.py's classify() tells us whether the room is genuinely
      # sitting between turns RIGHT NOW (state=="idle" in its vocabulary,
      # confusingly not the same "idle" as an idle ROOM, this is "idle
      # between turns of an active conversation") or mid-generation.
      local _state
      _state=$(REPO_ROOT="$REPO_ROOT" ROOM_DIR="$ROOMS_DIR/$chat_id" python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from pathlib import Path
from spawn_state import classify
print(classify(Path(os.environ['ROOM_DIR'])).get('state', ''))
" 2>/dev/null)
      if [[ "$_state" == "idle" ]]; then
        # Previously a long paste split by Telegram into several messages
        # could get answered after only the first fragment arrived.
        # The room is between turns AT THIS EXACT INSTANT, meaning claude
        # is about to read the very next inbox line. Injecting immediately
        # risks grabbing only the first fragment of a multi-message burst
        # before the rest lands, Telegram delivers a long paste's split
        # fragments a beat apart, not atomically, and if the first
        # fragment happens to coincide with a turn boundary claude answers
        # it alone before the sibling fragments even arrive. When the room
        # is mid-generation instead (tool/streaming/awaiting_result), this
        # race can't happen: claude won't read stdin again until the
        # CURRENT turn ends, giving every fragment time to land and
        # coalesce naturally. So only buffer in the "idle between turns"
        # case, give fragments the same short settle window the fully-
        # idle-ROOM path already uses (dispatch()'s bottom branch,
        # DEBOUNCE_WINDOW_S), just targeting inbox.ndjson instead of a
        # fresh spawn. drain_inject_burst (paired with drain_queue_if_idle)
        # flushes the whole coalesced burst as ONE inject_to_inbox call
        # once the window elapses.
        queue_line "$outgoing" "${USER_MSG_ID:-}" >> "$ROOMS_DIR/$chat_id/.inject_burst.jsonl"
        echo "$(( $(date +%s) + DEBOUNCE_WINDOW_S ))" > "$ROOMS_DIR/$chat_id/.inject_debounce_until"
        log "  buffered chat=$chat_id (inject debounce, between turns)"
      else
        inject_to_inbox "$outgoing" "$chat_id" "${USER_MSG_ID:-}"
        log "  injected chat=$chat_id (persistent inbox)"
      fi
      # 👀 used to fire unconditionally here, just
      # because .inbox_ready meant something was tailing the file, but
      # claude only actually checks stdin BETWEEN turns (see "Persistent
      # claude" above), so a message injected while claude is mid-tool-call
      # sits unread for however long the CURRENT turn's remaining work
      # takes, sometimes 20+ more tool calls and several minutes, nothing
      # like "seen, answering at the next gap". Only genuinely "idle between turns" earns 👀, true whether
      # we injected immediately or buffered it for the settle window above,
      # the message IS about to be seen either way. emoji_for_injected_state
      # (spawn_state.py) encodes this same mapping; reuse it directly on
      # the state we already fetched instead of a second python call.
      if [[ "$_state" == "idle" ]]; then
        react_emoji="👀"
      else
        react_emoji="🫡"
      fi
      [[ -z "$_state" ]] && react_emoji="🫡"   # classify() failed, fail toward the honest reaction
    else
      # Busy but no live inbox consumer: the spawn is tearing down, or a
      # silent one-shot is running. An inbox append here would be consumed
      # by nobody and truncated later, the message would silently vanish.
      # Queue it instead; drain_queue_if_idle coalesces and spawns it the
      # moment the room frees up. The queue is line-oriented, so embedded
      # newlines are stored as \x1e and restored by the drain.
      queue_line "$outgoing" "${USER_MSG_ID:-}" >> "$ROOMS_DIR/$chat_id/queue.jsonl"
      log "  queued chat=$chat_id (busy, inbox not ready)"
      # Distinct from 👀: this message ISN'T in front of claude yet, it's
      # sitting in queue.jsonl until the drain picks it up once the room
      # frees. Usually resolves within moments (the teardown window / a
      # silent one-shot in flight are both brief), but conflating it with
      # "seen" was misleading, 👀 implies claude already has it queued up
      # to answer at the very next gap, not "written down, will get to it."
      react_emoji="🫡"
    fi
    # 😴 overrides either outcome above: the room is on a rate-limit hold
    # (even during the brief window while the dying spawn still holds
    # busy.lock but has already written .rate_limited_until), so nothing
    # gets answered until the window clears regardless of which queue path
    # the message just took.
    if is_rate_limited "$ROOMS_DIR/$chat_id/.rate_limited_until"; then
      react_emoji="😴"
    fi
    if [[ -n "${USER_MSG_ID:-}" ]]; then
      CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" react "$USER_MSG_ID" "$react_emoji" 2>/dev/null || true
    fi
  elif is_rate_limited "$ROOMS_DIR/$chat_id/.rate_limited_until"; then
    # Room is parked on a 429 hold (session preserved). Spawning now would
    # just crash on the same limit, so queue the message for the
    # auto-resume and acknowledge with a sleepy react (the alert already
    # told the user when the room wakes up).
    queue_line "$outgoing" "${USER_MSG_ID:-}" >> "$ROOMS_DIR/$chat_id/queue.jsonl"
    log "  queued chat=$chat_id (rate-limit hold)"
    if [[ -n "${USER_MSG_ID:-}" ]]; then
      CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" react "$USER_MSG_ID" "😴" 2>/dev/null || true
    fi
  else
    rm -f "$ROOMS_DIR/$chat_id/.rate_limited_until"
    # Debounce instead of spawning immediately: buffer this message and
    # wait a short quiet period (DEBOUNCE_WINDOW_S, top of file) in case
    # more arrive right behind it. Every message that lands here is ALSO
    # dispatched through this exact branch (the room stays genuinely idle
    # the whole time, nothing spawns until the window elapses), so each
    # one appends to the same queue.jsonl and pushes the deadline forward.
    # drain_queue_if_idle (checked every loop iteration, promptly, see
    # the main loop's dynamic poll timeout below) coalesces the whole
    # burst into ONE turn once the quiet period is actually reached. This
    # is what fixes Telegram splitting one long paste into several
    # separate messages: without it, each fragment spawned its own turn,
    # claude answering a sentence cut off mid-thought before the rest of
    # it had even arrived.
    queue_line "$outgoing" "${USER_MSG_ID:-}" >> "$ROOMS_DIR/$chat_id/queue.jsonl"
    echo "$(( $(date +%s) + DEBOUNCE_WINDOW_S ))" > "$ROOMS_DIR/$chat_id/.debounce_until"
    log "  buffered chat=$chat_id (debounce window, room was idle)"
    # Acknowledge at once: the reply starts after the short quiet period
    # (plus a cold start), and a message with no reaction looks unseen.
    if [[ -n "${USER_MSG_ID:-}" ]]; then
      CHAT_ID="$chat_id" "$SCRIPTS/tg.sh" react "$USER_MSG_ID" "👀" 2>/dev/null || true
    fi
  fi
}

# --------------------------------- main loop ---------------------------------
OFFSET=0
[[ -f "$OFFSET_FILE" ]] && OFFSET=$(tr -d '\n\r ' < "$OFFSET_FILE")

log "Listener starting. Initial offset=$OFFSET"
# Baseline for auto-restart-on-stale-code (see maybe_auto_restart below),
# the listener's own equivalent of each spawn's .spawn_started_at.
date +%s > "$STATE_DIR/.listener_started_at"
# Which commit this listener process loaded, read by the dashboard to
# flag rooms whose own spawn is still running an older version.
code_version > "$STATE_DIR/.listener_version"
# If we were just restarted from the menu's ♻️ restart, tell the room that
# asked. Single-use + time-bounded (see lib.sh), so a crash-recovery boot
# won't fire a spurious "back up".
announce_restart_if_marked
# Prune stall forensic snapshots older than 7 days, they're only useful
# right after an incident; stale ones just consume disk.
find "$LOG_DIR/rooms" -name 'stall_*.txt' -mtime +7 -delete 2>/dev/null || true
FAIL_COUNT=0

any_room_busy() {
  local rd chat_id
  for rd in "$ROOMS_DIR"/*/; do
    [[ -d "$rd" ]] || continue
    rd="${rd%/}"
    chat_id="${rd##*/}"
    if is_busy "$chat_id"; then return 0; fi
  done
  return 1
}

any_room_debouncing() {
  local rd until now marker
  now=$(date +%s)
  for rd in "$ROOMS_DIR"/*/; do
    [[ -d "$rd" ]] || continue
    rd="${rd%/}"
    # Two independent debounce markers: .debounce_until (fully idle room,
    # queue.jsonl → drain_queue_if_idle) and .inject_debounce_until (busy
    # room caught between turns, .inject_burst.jsonl → drain_inject_burst).
    # Either one pending means we want the short poll timeout below.
    for marker in "$rd/.debounce_until" "$rd/.inject_debounce_until"; do
      [[ -f "$marker" ]] || continue
      until=$(cat "$marker" 2>/dev/null)
      [[ "$until" =~ ^[0-9]+$ ]] && (( now < until )) && return 0
    done
  done
  return 1
}

# Auto-restart-on-stale-code: the listener's own equivalent of the
# per-room stale-spawn refresh (dispatch()'s "stale spawn chat=..."
# check above), which only ever refreshed SPAWNS, a fresh spawn re-reads
# spawn_claude.sh/commands/*.sh from disk, but the LISTENER process
# itself just keeps running whatever version of listener.sh it loaded at
# its own last boot, with no equivalent self-check (a listener.sh edit
# could sit un-applied until a manual ♻️ restart). This closes that gap the same safe way the menu's manual
# restart already works, detached, delayed kickstart via
# with_setsid.py, so `launchctl kickstart -k` killing the listener's
# process group doesn't cascade to the restart trigger itself, just
# fired automatically instead of needing a button tap.
#
# Deliberately does NOT try to replay the CURRENT message through a
# freshly-restarted process (that would need killing mid-dispatch and
# racing the offset-advance write). The message in flight when staleness
# is noticed still answers on the old code, exactly as if you'd forgotten
# to restart, same as before this existed. The win is everything AFTER
# that: the very next message lands on a fresh listener automatically,
# no manual step required, matching how spawn refresh already only
# guarantees the NEXT turn, not the one that revealed the staleness.
#
# No visible badge, by design, same "invisible" precedent as spawn
# refresh (.stale_refresh). A cooldown marker prevents re-firing the
# detached trigger on every loop iteration while one is already pending
# (the actual kickstart lands a couple seconds later, not instantly).
AUTO_RESTART_COOLDOWN_S="$(bot_config BOT_AUTO_RESTART_COOLDOWN_S 15)"

# Throttled (found chasing intermittent "listener goes quiet" stalls under
# the e2e suite's full sequential run): code_mtime() forks a
# SEPARATE stat process per matching file in scripts/+hooks/ (`find
# -exec ... \;`, not batched), 40-50+ forks per call on this repo. The
# per-room stale-spawn check only ever paid that cost once per DISPATCHED
# MESSAGE (a comparatively rare event). Calling it here unthrottled meant
# paying it on EVERY main-loop iteration instead, including the tight
# ~1s cadence while any room is mid-debounce, which was enough added
# fork/exec overhead under load to occasionally starve the loop for many
# seconds. A staleness check has no need for that precision: noticing a
# code change a few seconds late is irrelevant here, so it's throttled
# to run at most once per AUTO_RESTART_CHECK_INTERVAL_S regardless of how
# often the loop itself cycles.
AUTO_RESTART_CHECK_INTERVAL_S="$(bot_config BOT_AUTO_RESTART_CHECK_INTERVAL_S 5)"
_last_stale_check_at=0

listener_code_stale() {
  local started newest
  started=$(cat "$STATE_DIR/.listener_started_at" 2>/dev/null)
  [[ "$started" =~ ^[0-9]+$ ]] || return 1
  newest=$(code_mtime)
  [[ "$newest" =~ ^[0-9]+$ ]] || return 1
  (( newest > started ))
}

maybe_auto_restart() {
  local _now_check
  _now_check=$(date +%s)
  if (( _now_check - _last_stale_check_at < AUTO_RESTART_CHECK_INTERVAL_S )); then
    return 0
  fi
  _last_stale_check_at=$_now_check
  listener_code_stale || return 0
  local marker="$STATE_DIR/.auto_restart_triggered"
  local last now
  now=$(date +%s)
  last=$(cat "$marker" 2>/dev/null)
  if [[ "$last" =~ ^[0-9]+$ ]] && (( now - last < AUTO_RESTART_COOLDOWN_S )); then
    return 0   # already triggered recently; the kickstart is on its way
  fi
  echo "$now" > "$marker"
  log "  listener code changed since boot (started=$(cat "$STATE_DIR/.listener_started_at" 2>/dev/null) code_mtime=$(code_mtime)), auto-restarting (detached)"
  emit_metric restart_auto
  RESTART_CMD="${BOT_RESTART_CMD:-$REPO_ROOT/scripts/safe_restart.sh --force}"
  RESTART_DELAY="${BOT_RESTART_DELAY:-2}"
  python3 "$REPO_ROOT/scripts/with_setsid.py" \
    bash -c "sleep $RESTART_DELAY; exec $RESTART_CMD" \
    </dev/null >>"$LOG" 2>&1 &
  disown 2>/dev/null || true
}

# Pick a ~/Downloads path that never clobbers an existing file: name.ext,
# then name_1.ext, name_2.ext... Belt and braces under the per-message
# media names, and protects re-sent documents that share a filename
# (per-second names once let a photo burst overwrite itself).
unique_download_dest() {
  local name="$1" dir="$HOME/Downloads" stem ext n=1
  local dest="$dir/$name"
  if [[ "$name" == *.* ]]; then stem="${name%.*}"; ext=".${name##*.}"; else stem="$name"; ext=""; fi
  while [[ -e "$dest" ]]; do
    dest="$dir/${stem}_${n}${ext}"; n=$((n + 1))
  done
  printf '%s' "$dest"
}

while true; do
  maybe_auto_restart
  # Short timeout while any room is busy so we react quickly to /cancel and
  # queueing; shorter still while any room is mid-debounce so the quiet
  # period (dispatch()'s idle branch, DEBOUNCE_WINDOW_S at top of file) is
  # noticed promptly instead of sitting out a long idle poll; long
  # otherwise to be kind to Telegram. Debouncing is checked FIRST and wins
  # over plain busy: the busy+ready inject-burst debounce (.inject_burst /
  # .inject_debounce_until) fires precisely while a room is busy, so
  # checking any_room_busy first would always shadow it and leave that
  # settle window sitting out the full 5s busy poll instead of being
  # noticed within 1s like every other debounce case.
  if any_room_debouncing; then
    TIMEOUT=1
  elif any_room_busy; then
    TIMEOUT=5
  else
    TIMEOUT=30
  fi

  RESPONSE=$(curl -s --max-time $((TIMEOUT + 5)) \
    "${TELEGRAM_API_BASE}/bot${TELEGRAM_BOT_TOKEN}/getUpdates?offset=${OFFSET}&timeout=${TIMEOUT}" 2>/dev/null)
  CURL_EXIT=$?

  if [[ $CURL_EXIT -ne 0 || -z "$RESPONSE" ]]; then
    FAIL_COUNT=$((FAIL_COUNT + 1))
    if [[ $((FAIL_COUNT % 20)) -eq 1 ]]; then
      log "Network failure #$FAIL_COUNT (curl exit $CURL_EXIT). Transient network drop."
    fi
    # NO fire_scheduled_tasks here: check_schedule.py consumes a due task
    # from scheduled.json the moment it reads it, and a claude fired into a
    # known-dead network just crashes with the task already gone (and the
    # failure alert can't send either). Tasks fire on the next successful
    # poll instead, which the backoff below reaches within seconds.
    drain_queue_if_idle
    drain_inject_burst
    # Exponential backoff (2,4,8,16 then 30s cap) instead of a flat 30s,
    # so a single blip recovers in seconds, not half a minute.
    sleep $(( FAIL_COUNT >= 5 ? 30 : (1 << FAIL_COUNT) ))
    continue
  fi
  if [[ $FAIL_COUNT -gt 0 ]]; then
    log "Network recovered after $FAIL_COUNT failures."
    FAIL_COUNT=0
  fi
  # Self-heal a missing bot username (boot-time getMe failure) now that the
  # network demonstrably works. At most one attempt per 10 minutes.
  if [[ -z "$BOT_USERNAME" ]] && (( $(date +%s) >= BOT_USERNAME_RETRY_AT )); then
    BOT_USERNAME_RETRY_AT=$(( $(date +%s) + 600 ))
    BOT_USERNAME=$(fetch_bot_username)
    if [[ -n "$BOT_USERNAME" ]]; then
      echo "$BOT_USERNAME" > "$BOT_USERNAME_CACHE" 2>/dev/null || true
      log "bot username resolved late: @$BOT_USERNAME (mention gate active)"
    fi
  fi

  PARSED=$(RESPONSE="$RESPONSE" SEEN_FILE="$STATE_DIR/.seen_msgs.json" python3 <<'PYEOF'
import hashlib, json, os, sys, time


def dname(s):
    # Sender display name for the "[Name]:" message prefix. Strip field/record
    # separators + newlines so it can never break the TSV; cap the length.
    n = (s.get('first_name') or s.get('username') or '').strip()
    for ch in ('\x1f', '\x1e', '\n', '\r'):
        n = n.replace(ch, ' ')
    return n[:32]


# Fingerprints of messages already handled, keyed "chat:message_id". Telegram
# sends bots an edited_message for a group message when someone REACTS to it,
# with nothing actually changed. Treating those as new messages re-sent days
# old texts to Claude. An edit whose content matches what we
# already saw, or one for an old message we never fingerprinted, is dropped.
SEEN_FILE = os.environ.get('SEEN_FILE', '')
SEEN_CAP = 5000
EDIT_UNKNOWN_MAX_AGE_S = 600
try:
    seen = json.load(open(SEEN_FILE)) if SEEN_FILE and os.path.exists(SEEN_FILE) else {}
    if not isinstance(seen, dict):
        seen = {}
except Exception:
    seen = {}
seen_dirty = False


def fingerprint(m):
    parts = [m.get('text') or '', m.get('caption') or '']
    for k in ('photo', 'document', 'video', 'audio', 'voice', 'video_note',
              'animation', 'sticker'):
        v = m.get(k)
        if isinstance(v, list) and v:
            v = v[-1]
        if isinstance(v, dict):
            parts.append(v.get('file_unique_id') or '')
    return hashlib.sha1('\x00'.join(parts).encode('utf-8', 'replace')).hexdigest()[:16]


def save_seen():
    if not (SEEN_FILE and seen_dirty):
        return
    items = list(seen.items())[-SEEN_CAP:]
    tmp = SEEN_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(dict(items), f)
        os.replace(tmp, SEEN_FILE)
    except Exception:
        pass


try:
    raw = os.environ.get('RESPONSE', '')
    d = json.loads(raw) if raw else {}
    if not d.get('ok'):
        print('ERR', d.get('description', 'unknown' if d else 'empty response'))
        sys.exit(0)
    for u in d.get('result', []):
        # callback_query update (inline-keyboard button tap)
        if 'callback_query' in u:
            cb = u['callback_query']
            cb_id = cb.get('id', '')
            sender = cb.get('from') or {}
            cb_msg = cb.get('message') or {}
            chat = cb_msg.get('chat') or {}
            cb_msg_id = cb_msg.get('message_id', '')
            data = (cb.get('data') or '').replace('\x1f', ' ').replace('\n', '\x1e').replace('\r', '')
            # TSV: update_id | chat | from_user | from_name | kind | cb_id | cb_msg_id | text
            print(f"{u['update_id']}\x1f{chat.get('id', 0)}\x1f{sender.get('id', 0)}\x1f{dname(sender)}\x1fcb\x1f{cb_id}\x1f{cb_msg_id}\x1f{data}")
            continue
        is_edit = 'message' not in u and 'edited_message' in u
        msg = u.get('message') or u.get('edited_message') or {}
        chat = msg.get('chat') or {}
        sender = msg.get('from') or {}
        skey = f"{chat.get('id', 0)}:{msg.get('message_id', '')}"
        sig = fingerprint(msg)
        if is_edit:
            age = time.time() - (msg.get('date') or time.time())
            if seen.get(skey) == sig or (skey not in seen and age > EDIT_UNKNOWN_MAX_AGE_S):
                print(f"{u['update_id']}\x1f{chat.get('id', 0)}\x1f{sender.get('id', 0)}\x1f{dname(sender)}\x1fedit_dup\x1f\x1f{msg.get('message_id', '')}\x1f")
                continue
        if seen.get(skey) != sig:
            seen.pop(skey, None)
            seen[skey] = sig
            seen_dirty = True
        # Native contact-picker result (KeyboardButtonRequestUsers). The owner
        # tapped the Users menu's "Add user" → picked a contact → Telegram
        # sends users_shared (Bot API 7.0+; the older singular user_shared is
        # handled too). Emit the picked user_id in the cb_msg_id slot and the
        # @username (alnum/underscore only, so TSV-safe) in the text slot;
        # sender is the OWNER, so the allowlist gate lets it through.
        us = msg.get('users_shared') or msg.get('user_shared')
        if us:
            picked = us.get('users') or []
            if picked and isinstance(picked[0], dict):
                puid = picked[0].get('user_id', 0)
                puname = picked[0].get('username') or ''
            else:
                ids = us.get('user_ids') or picked or []
                puid = ids[0] if ids else us.get('user_id', 0)
                puname = ''
            puname = ''.join(c for c in str(puname) if c.isalnum() or c == '_')
            print(f"{u['update_id']}\x1f{chat.get('id', 0)}\x1f{sender.get('id', 0)}\x1f{dname(sender)}\x1fusers_shared\x1f\x1f{puid}\x1f{puname}")
            continue
        msg_id = msg.get('message_id', '')
        text = (msg.get('text') or '').replace('\x1f', ' ').replace('\n', '\x1e').replace('\r', '')
        # If text empty, classify the media type so the listener can give a
        # useful reply rather than silently dropping voice/photo/sticker/etc.
        kind = 'text'
        if not text:
            for k in ('photo','voice','audio','video','video_note','document',
                      'sticker','animation','location','venue','contact','poll',
                      'dice'):
                if msg.get(k):
                    kind = f'media:{k}'
                    if k == 'document':
                        doc = msg.get('document', {})
                        fid = doc.get('file_id', '')
                        fname = (doc.get('file_name') or 'file').replace('\x1f', '_').replace('|', '_')
                        text = f'{fid}|{fname}'
                    elif k in ('video', 'video_note', 'animation', 'audio', 'voice'):
                        # Saved to ~/Downloads exactly like a document. These
                        # used to fall through to the "I only understand
                        # text" reply. Most
                        # carry no file_name, so build one from the mime
                        # type; message_id keeps an album's items (which
                        # share one timestamp) from overwriting each other.
                        m = msg.get(k, {})
                        fid = m.get('file_id', '')
                        ext = {'video/mp4': '.mp4', 'video/quicktime': '.mov',
                               'video/webm': '.webm', 'audio/mpeg': '.mp3',
                               'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a',
                               'audio/ogg': '.ogg', 'audio/wav': '.wav',
                               'audio/x-wav': '.wav'}.get(
                                   m.get('mime_type', ''),
                                   '.ogg' if k == 'voice' else
                                   '.mp3' if k == 'audio' else '.mp4')
                        stem = {'video_note': 'videonote'}.get(k, k)
                        fname = m.get('file_name') or f"{stem}_{msg.get('date', 0)}_{msg_id}{ext}"
                        fname = fname.replace('\x1f', '_').replace('|', '_').replace('/', '_')
                        text = f'{fid}|{fname}'
                    elif k == 'photo':
                        photos = msg.get('photo', [])
                        if photos:
                            best = photos[-1]
                            fid = best.get('file_id', '')
                            ts = msg.get('date', 0)
                            fname = f'photo_{ts}_{msg_id}.jpg'
                            caption = (msg.get('caption') or '').replace('\x1f', ' ').replace('|', '│').replace('\n', ' ').replace('\r', '')
                            text = f'{fid}|{fname}|{caption}'
                    break
            else:
                kind = 'unknown'
        # Thread the user's message_id in the cb_msg_id slot for text
        # messages so dispatch() can react to it (👀 emoji) when injecting
        # into a busy persistent spawn.
        print(f"{u['update_id']}\x1f{chat.get('id', 0)}\x1f{sender.get('id', 0)}\x1f{dname(sender)}\x1f{kind}\x1f\x1f{msg_id}\x1f{text}")
    save_seen()
except Exception as e:
    print('ERR', str(e))
PYEOF
)

  if [[ "$PARSED" == ERR* ]]; then
    log "parse failed: $PARSED"
    sleep 5
    continue
  fi

  if [[ -n "$PARSED" ]]; then
    while IFS=$'\x1f' read -r UPDATE_ID FROM_CHAT FROM_USER FROM_NAME KIND CB_ID CB_MSG_ID TEXT; do
      [[ -z "$UPDATE_ID" ]] && continue
      CALLBACK_MSG_ID=""

      if [[ ! "$UPDATE_ID" =~ ^[0-9]+$ ]]; then
        log "  skip: non-numeric update_id (parse failure): '${UPDATE_ID:0:60}'"
        continue
      fi

      if ! is_valid_chat_id "$FROM_CHAT"; then
        # Catches "0" (parser default when chat.id is missing) AND obvious
        # test placeholders (123456, 999, etc) that should never reach the
        # room-dir creators. Without this, a malformed update or a botched
        # test could leave a state/rooms/<bogus>/ ghost forever.
        log "  skip: invalid chat_id='${FROM_CHAT:0:32}'"
        continue
      fi

      log "update_id=$UPDATE_ID chat=$FROM_CHAT from=$FROM_USER kind=$KIND text='${TEXT:0:120}'"
      if [[ "$KIND" == "edit_dup" ]]; then
        # Unchanged "edit" (Telegram sends these when someone reacts to a
        # group message). Never a new message; see SEEN_FILE in the parser.
        log "  skip: edited_message msg_id=$CB_MSG_ID with no new content (reaction or stale edit)"
        echo "$((UPDATE_ID + 1))" > "$OFFSET_FILE"; OFFSET=$((UPDATE_ID + 1))
        continue
      fi
      # Offset commit is deferred to AFTER this update is dispatched
      # (search for "advance offset" below). Pre-fix this advanced the
      # cursor BEFORE dispatch, so a crash mid-processing silently lost
      # the message (Telegram does not redeliver past a moved cursor).
      NEW_OFFSET=$((UPDATE_ID + 1))

      # Allowlist on SENDER's user_id, not chat_id. The owner (TELEGRAM_CHAT_ID)
      # and any static TELEGRAM_ALLOWED_IDS are trusted in every room; extra
      # users approved from the Users menu are trusted only in the specific
      # room they were approved for (per-room scope, read live from
      # approved_users.json). The bot routes replies by chat_id but trusts
      # based on from_id, so an allowed user talks to the bot from the chats
      # they share with it. Whole-token matches, so "0"/anonymous senders never
      # slip through.
      refresh_approved
      if ! sender_allowed "$FROM_CHAT" "$FROM_USER"; then
        log "  skip: sender $FROM_USER not allowlisted (chat=$FROM_CHAT)"
        # Capture the blocked sender as PENDING for this room so the owner can
        # one-tap approve them from 👥 users (the native contact picker can't
        # reliably surface group-only members). Valid positive ids only, never
        # anonymous "0". Idempotent + capped in approved_users.py; best-effort.
        if [[ "$FROM_USER" =~ ^[1-9][0-9]*$ ]]; then
          python3 "$SCRIPTS/approved_users.py" pending-add "$FROM_CHAT" "$FROM_USER" >/dev/null 2>&1 || true
        fi
        echo "$NEW_OFFSET" > "$OFFSET_FILE"; OFFSET=$NEW_OFFSET
        continue
      fi
      if [[ "$KIND" == "users_shared" ]]; then
        # Contact-picker result: the owner picked a user to approve for THIS
        # room (FROM_CHAT). CB_MSG_ID slot = picked user_id, TEXT = @username
        # (may be empty). Hand to the users handler, which writes the live
        # approved_users.json and confirms. Runs BEFORE the empty-text gate
        # because a picked user with no @username has an empty TEXT slot.
        if [[ "$CB_MSG_ID" =~ ^[1-9][0-9]*$ ]]; then
          log "  users_shared chat=$FROM_CHAT approve uid=$CB_MSG_ID uname='${TEXT}'"
          export CHAT_ID="$FROM_CHAT"
          CALLBACK_MSG_ID="" invoke_action "users approve $CB_MSG_ID $TEXT" "$FROM_CHAT"
        else
          log "  users_shared chat=$FROM_CHAT: ignoring bad picked uid '${CB_MSG_ID:0:32}'"
        fi
        echo "$NEW_OFFSET" > "$OFFSET_FILE"; OFFSET=$NEW_OFFSET
        continue
      fi
      if [[ "$KIND" =~ ^media:(document|video|video_note|animation|audio|voice)$ && -n "$TEXT" ]]; then
        # Download the file to ~/Downloads using its original filename
        # (or a generated one for media that doesn't carry a name).
        DL_FILE_ID="${TEXT%%|*}"
        DL_FILE_NAME="${TEXT#*|}"
        DL_DEST="$(unique_download_dest "$DL_FILE_NAME")"
        DL_FILE_NAME="$(basename "$DL_DEST")"
        DL_KIND="${KIND#media:}"
        case "$DL_KIND" in
          video|video_note|animation) DL_ICON="🎬" ;;
          audio|voice)                DL_ICON="🎵" ;;
          *)                          DL_ICON="📁" ;;
        esac
        log "  ${DL_KIND}: file_id=${DL_FILE_ID} name=${DL_FILE_NAME}"
        DL_ERR_TMP=$(mktemp)
        if CHAT_ID="$FROM_CHAT" "$SCRIPTS/tg.sh" download_file "$DL_FILE_ID" "$DL_DEST" >>"$LOG" 2>"$DL_ERR_TMP"; then
          CHAT_ID="$FROM_CHAT" "$SCRIPTS/tg.sh" send "${DL_ICON} Saved **${DL_FILE_NAME}** to ~/Downloads." >>"$LOG" 2>&1 || true
          log "  ${DL_KIND}: saved $DL_DEST"
        else
          DL_REASON=$(cat "$DL_ERR_TMP" | sed 's/tg\.sh: //' | head -1)
          # The cloud Bot API refuses getFile above 20 MB, which most phone
          # videos exceed. Say that plainly instead of the raw API error.
          if grep -qi "file is too big" "$DL_ERR_TMP"; then
            DL_REASON="it's over Telegram's 20 MB limit for bot downloads. Send it another way (for example as a link)"
          fi
          CHAT_ID="$FROM_CHAT" "$SCRIPTS/tg.sh" send "❌ Couldn't save **${DL_FILE_NAME}**: ${DL_REASON:-unknown error}" >>"$LOG" 2>&1 || true
          log "  ${DL_KIND}: download failed for $DL_FILE_NAME: $DL_REASON"
        fi
        rm -f "$DL_ERR_TMP"
        echo "$NEW_OFFSET" > "$OFFSET_FILE"; OFFSET=$NEW_OFFSET
        continue
      fi
      if [[ "$KIND" == "media:photo" && -n "$TEXT" ]]; then
        # Photo: file_id|filename|caption (caption may be empty)
        DL_FILE_ID="${TEXT%%|*}"
        DL_REST="${TEXT#*|}"
        DL_FILE_NAME="${DL_REST%%|*}"
        PHOTO_CAPTION="${DL_REST#*|}"
        [[ "$PHOTO_CAPTION" == "$DL_FILE_NAME" ]] && PHOTO_CAPTION=""
        DL_DEST="$(unique_download_dest "$DL_FILE_NAME")"
        DL_FILE_NAME="$(basename "$DL_DEST")"
        log "  photo: file_id=${DL_FILE_ID} name=${DL_FILE_NAME} caption='${PHOTO_CAPTION:0:60}'"
        DL_ERR_TMP=$(mktemp)
        if ! CHAT_ID="$FROM_CHAT" "$SCRIPTS/tg.sh" download_file "$DL_FILE_ID" "$DL_DEST" >>"$LOG" 2>"$DL_ERR_TMP"; then
          DL_REASON=$(sed 's/tg\.sh: //' "$DL_ERR_TMP" | head -1)
          CHAT_ID="$FROM_CHAT" "$SCRIPTS/tg.sh" send "❌ Couldn't save that photo: ${DL_REASON:-Telegram sent no file}. Try sending it again." >>"$LOG" 2>&1 || true
          log "  photo: download failed: $DL_REASON"
          rm -f "$DL_ERR_TMP"
          echo "$NEW_OFFSET" > "$OFFSET_FILE"; OFFSET=$NEW_OFFSET
          continue
        fi
        rm -f "$DL_ERR_TMP"
        log "  photo: saved $DL_DEST"
        # Every photo (captioned or not, every photo of an album) goes to
        # Claude as an ordinary message through dispatch() below, so it gets
        # the same queueing, burst debounce (an album's photos arrive a beat
        # apart and coalesce into one turn), reactions and spawn handling as
        # text. Claude opens the saved file with its Read tool. BUG FIXED:
        # captioned photos used to be written straight into inbox.ndjson
        # (no .pending_reacts line, so every later reaction landed one
        # message off; dropped when no spawn was reading), and uncaptioned
        # photos or the rest of an album never reached Claude at all.
        PHOTO_NOTE="[Photo saved at ${DL_DEST}. Open it with the Read tool to see it.]"
        if [[ -n "$PHOTO_CAPTION" ]]; then
          TEXT="${PHOTO_CAPTION}"$'\n\n'"${PHOTO_NOTE}"
        else
          TEXT="$PHOTO_NOTE"
        fi
      fi
      if [[ -z "$TEXT" ]]; then
        # Non-text message (photo, voice, sticker, etc). Reply once per
        # room so the user knows the bot is text-only, better than the
        # old "silent drop" behaviour that left the user wondering why
        # nothing happened.
        if [[ "$KIND" == media:* ]]; then
          # NB: `local` is illegal outside a function; this block runs in
          # the main shell. Use plain vars.
          MEDIA_NAME="${KIND#media:}"
          NAG_MARKER="$ROOMS_DIR/$FROM_CHAT/.non_text_nag"
          if [[ ! -f "$NAG_MARKER" ]] || \
             [[ $(($(date +%s) - $(stat -f %m "$NAG_MARKER" 2>/dev/null || echo 0))) -gt 3600 ]]; then
            mkdir -p "$ROOMS_DIR/$FROM_CHAT" 2>/dev/null
            CHAT_ID="$FROM_CHAT" "$SCRIPTS/tg.sh" send "📨 Got your ${MEDIA_NAME}, but I only understand text right now. Send a text message instead." >>"$LOG" 2>&1 || true
            touch "$NAG_MARKER"
          fi
          log "  skip: $KIND (non-text reply sent or rate-limited)"
        else
          log "  skip: $KIND"
        fi
        echo "$NEW_OFFSET" > "$OFFSET_FILE"; OFFSET=$NEW_OFFSET
        continue
      fi

      TEXT="${TEXT//$'\x1e'/$'\n'}"

      if [[ "$KIND" == "cb" ]]; then
        # Inline-keyboard tap. Ack so the spinner goes away first.
        CHAT_ID="$FROM_CHAT" "$SCRIPTS/tg.sh" answer_cb "$CB_ID" >/dev/null 2>&1 || true
        # Callback wire format:
        #   act:<cmd>[ <args>]   → real action (handled by dispatch)
        #   decor:*              → inert (section headers, empty-state
        #                          placeholders); ack and drop here
        # Anything else is a stale button from before the act: migration
        # or a third-party trying to fuzz callback_data, drop loudly.
        if [[ "$TEXT" == decor:* ]]; then
          log "  callback chat=$FROM_CHAT decorative='${TEXT:0:60}' (silent)"
          echo "$NEW_OFFSET" > "$OFFSET_FILE"; OFFSET=$NEW_OFFSET
          continue
        fi
        if [[ "$TEXT" != act:* ]]; then
          log "  callback chat=$FROM_CHAT unknown_format='${TEXT:0:60}' (dropped)"
          echo "$NEW_OFFSET" > "$OFFSET_FILE"; OFFSET=$NEW_OFFSET
          continue
        fi
        log "  callback chat=$FROM_CHAT msg=$CB_MSG_ID data='${TEXT:0:120}'"
        CALLBACK_MSG_ID="$CB_MSG_ID" dispatch "$TEXT" "$FROM_CHAT"
      else
        # Thread the user's message_id through so dispatch can react
        # to it (👀) when injecting into a busy persistent spawn.
        USER_MSG_ID="$CB_MSG_ID" dispatch "$TEXT" "$FROM_CHAT" "$FROM_NAME"
      fi

      # advance offset, only after the dispatch above completes. If we
      # crash before this, the next getUpdates will redeliver this update
      # rather than us silently losing it.
      echo "$NEW_OFFSET" > "$OFFSET_FILE"
      OFFSET=$NEW_OFFSET
    done <<< "$PARSED"
  fi

  # Check for scheduled tasks due to fire.
  fire_scheduled_tasks
  # After processing any incoming batch, see if the busy slot freed up while
  # we were polling, and if so dequeue the next message.
  drain_queue_if_idle
  drain_inject_burst
  rescue_missed_wakeups
done
