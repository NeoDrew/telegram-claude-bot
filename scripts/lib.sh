#!/bin/bash
# lib.sh - shared bash helpers, sourced by listener.sh, handle_action.sh,
# spawn_claude.sh, and tg.sh. NEVER execute directly; always source.
#
# Conventions:
#   - All helpers are pure functions of their arguments + environment.
#   - $REPO_ROOT must be set by the caller before sourcing.

# Pull a single KEY=value line out of $REPO_ROOT/.env without bulk-sourcing
# (which would leak ANTHROPIC_API_KEY into spawned claudes).
extract_var() {
  awk -F= -v key="^$1=" '$0 ~ key {sub(key, ""); print; exit}' "$REPO_ROOT/.env"
}

# Machine-specific settings. Each comes from the environment first, then a
# KEY=value line in .env (via extract_var, never bulk-sourced), then a
# default, so a fresh checkout runs with no config beyond the tokens. Every
# key is listed in .env.example; scripts/bot_config.py is the Python twin.
bot_config() {
  local key="$1" default="${2:-}" v
  v="${!key:-}"
  [[ -z "$v" && -f "$REPO_ROOT/.env" ]] && v=$(extract_var "$key")
  printf '%s' "${v:-$default}"
}
# launchd label of the listener job; the dashboard job adds "-dashboard".
bot_label()    { bot_config BOT_LAUNCHD_LABEL com.telegram-claude-bot; }
# Where the cd menus look for projects, and a room's default cwd. Default: home.
projects_dir() { local d; d=$(bot_config BOT_PROJECTS_DIR "$HOME"); printf '%s' "${d/#\~/$HOME}"; }
# The phone notifier. Ships in the repo; ~/.local/bin/notify-cmd can link to it.
notify_cmd()   { local c; c=$(bot_config NOTIFY_CMD "$REPO_ROOT/scripts/notify-cmd"); printf '%s' "${c/#\~/$HOME}"; }
# Claude Code binary (the e2e harness points this at a fake).
claude_bin()   { local c; c=$(bot_config CLAUDE_BIN_OVERRIDE "$HOME/.local/bin/claude"); printf '%s' "${c/#\~/$HOME}"; }
# The assistant's display name, used in its system prompt and bot messages.
bot_name()     { bot_config BOT_NAME Claude; }
# Python used to build .venv (screenshots and window capture).
bot_python()   { bot_config BOT_PYTHON "$(command -v python3)"; }

# Human-readable elapsed/remaining time, e.g. "90" -> "1h30m", "45" -> "45m",
# "100000" -> "1d3h". Deliberately coarse (no seconds), this is a
# contextual nudge, not a stopwatch. Shared by listener.sh's time-gap
# message annotation ("[Alice, Telegram, +1h15m later]: ...") and
# spawn_claude.sh's rate-limit notifications ("auto-resuming at 5:00 PM,
# in 1h15m").
format_gap() {
  local secs="$1" d h m
  d=$(( secs / 86400 ))
  h=$(( (secs % 86400) / 3600 ))
  m=$(( (secs % 3600) / 60 ))
  if (( d > 0 )); then
    (( h > 0 )) && echo "${d}d${h}h" || echo "${d}d"
  elif (( h > 0 )); then
    (( m > 0 )) && echo "${h}h${m}m" || echo "${h}h"
  else
    echo "${m}m"
  fi
}

# Safely check whether a room is still inside its rate-limit hold.
#
# Usage: is_rate_limited "$rd/.rate_limited_until" && ...
#
# Returns 0 (true, still limited) when the file holds a valid future epoch,
# OR when the file exists but its content isn't a clean integer (empty,
# truncated, garbled). Returns 1 (false, not limited) only when the file is
# genuinely absent or holds a valid past epoch.
#
# Why the "garbled -> still limited" default matters:
# the writer does `echo "$RESUME_AT" > file`, a non-atomic truncate-then-
# write. A reader that catches the file mid-write sees it empty. The naive
# comparison `(( now < $(cat file || echo 0) ))` treats an empty command
# substitution as an arithmetic syntax error, which bash's `if` silently
# takes as false, i.e. "not limited", which deletes the hold and fires an
# immediate premature resume into a limit that hasn't actually cleared.
# That reads as a room spamming retries forever; the actual bug is a
# transient read racing a transient write. Defaulting an unparsable read to
# "still limited" costs at most one extra poll cycle (a few seconds) before
# the next read sees the real value; defaulting it the other way costs a
# wasted spawn and a spurious notification every time the race is lost.
is_rate_limited() {
  local f="$1" val
  [[ -f "$f" ]] || return 1
  val=$(cat "$f" 2>/dev/null)
  [[ "$val" =~ ^[0-9]+$ ]] || return 0
  (( $(date +%s) < val ))
}

# Is this a plausible Telegram chat_id?
#
# Usage: is_valid_chat_id "$chat_id" && ...
#
# Rejects empty / non-integer / "0" / abs(value) < 1,000,000. The magnitude
# threshold catches obvious test placeholders like 123456 from leaking into
# state/rooms/<id>/. Real Telegram bot users have user_ids well above 10^6
# (typically 9-10 digits); supergroups/topics are 13-digit negatives;
# the smallest non-super groups in the wild are still 9+ digits. A truly
# ancient single-digit user_id is theoretically possible but would never
# message THIS bot (allowlisted to one owner anyway), so the false-rejection
# rate is zero in practice.
#
# Called at every site that would `mkdir -p state/rooms/$chat_id`:
#   - listener.sh's update parser (gate before dispatch)
#   - handle_action.sh's ROOM_DIR init
#   - spawn_claude.sh's ROOM_DIR init
# Defence in depth: any single missing call still gets caught by the others.
is_valid_chat_id() {
  local id="${1:-}"
  [[ -z "$id" ]] && return 1
  # Strict signed-integer match, no leading zeros, no whitespace, no dots.
  [[ "$id" =~ ^-?[1-9][0-9]*$ ]] || return 1
  # Magnitude check via shell arithmetic (works for both signs).
  local abs=$id
  [[ "$abs" == -* ]] && abs="${abs#-}"
  (( abs >= 1000000 ))
}

# Recursive process-tree kill. pkill -P only reaches direct children, so
# on a multi-stage pipeline (`tail -f | claude | streamer`) claude itself
# is a grandchild and survives a -P kill. kill_tree walks the descendant
# tree and signals everyone before the root.
kill_tree() {
  local p=$1 sig=${2:-TERM}
  local c
  for c in $(pgrep -P "$p" 2>/dev/null); do
    kill_tree "$c" "$sig"
  done
  kill -"$sig" "$p" 2>/dev/null || true
}

# Is the named room's persistent spawn alive?
#
# Usage: is_busy <chat_id>
#
# Returns 0 (busy) if either:
#   - busy.lock exists AND its pid is still alive, OR
#   - the inbox NDJSON is being tail -f'd (zombie spawn, lock racy-cleaned
#     but the wrapper is still consuming injected messages).
#
# Clears a stale busy.lock as a side effect (pid is gone → lock is dead).
#
# Sourced into listener.sh (router) AND handle_action.sh per-command
# handlers (cancel.sh in particular), so the same definition of "busy"
# governs routing decisions and the cancel-confirm prompt. Requires
# $REPO_ROOT to be set.
is_busy() {
  local chat_id="$1"
  local rd="$REPO_ROOT/state/rooms/$chat_id"
  local lock="$rd/busy.lock"
  local inbox="$rd/inbox.ndjson"
  # No lock at all → not busy, full stop. The pgrep fallback used to run
  # here too, which meant up to ~3 full process-table scans per room per
  # poll iteration in the common all-idle steady state. A lockless zombie
  # tail (wrapper SIGKILLed AND lock already gone) is instead caught by
  # spawn_claude.sh's single-spawn guard, which reaps orphaned tails.
  [[ -f "$lock" ]] || return 1
  local p
  p=$(cat "$lock" 2>/dev/null)
  if [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; then
    return 0
  fi
  rm -f "$lock"
  # Lock was stale (wrapper died without its EXIT trap, e.g. SIGKILL). The
  # pipeline may still be alive and consuming the inbox, so check for its
  # tail before declaring idle. Escape regex metachars in $inbox so chat
  # ids that are NUMERIC PREFIXES of other chat ids don't cross-match.
  # Anchor with $ so we match the inbox path as a suffix only.
  if [[ -f "$inbox" ]]; then
    local inbox_re tpid
    inbox_re=$(printf '%s' "$inbox" | sed 's/[][\\.*^$()+?{|/]/\\&/g')
    tpid=$(pgrep -f "tail -n[^|]* -f ${inbox_re}\$" 2>/dev/null | head -1)
    if [[ -n "$tpid" ]]; then
      # HEAL the lock onto the surviving pipeline (the tail's pid). Without
      # this, the next call would hit the lockless fast path above and
      # declare the room idle while an orphaned claude still runs, the
      # double-spawn-same-session bug. The healed lock goes stale again
      # naturally when the pipeline dies.
      echo "$tpid" > "$lock"
      return 0
    fi
  fi
  return 1
}

# Reap orphaned inbox pipelines for a room; detect live siblings.
#
# Usage: reap_inbox_pipeline <chat_id>
#
# Returns 0 when the room's inbox has NO live consumer left (safe to seed
# and spawn). Returns 1 when a live sibling spawn still owns it (caller
# must NOT truncate or spawn). An orphan is a tail whose parent wrapper is
# gone (reparented to pid 1): nothing will ever tear it down, and its
# downstream claude keeps burning tokens. We kill the orphan's whole
# process group, the dead wrapper's setsid group holds tail AND claude,
# never just the tail (killing only the tail EOFs claude but can leave it
# mid-turn, still costing a full completion).
# _diag_ts: millisecond-precision UTC timestamp for diagnostic logging only.
# `date` on macOS's stock /bin/bash (3.2, what launchd actually runs this
# under) has no %3N/%N support, so this shells out to python3. Never use
# this in a hot per-poll path, reap_inbox_pipeline and start_spawn are
# both once-per-spawn-attempt call sites, so the cost is negligible there.
_diag_ts() {
  python3 -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]+'Z')" 2>/dev/null || date -u +%FT%TZ
}

reap_inbox_pipeline() {
  local chat_id="$1"
  local inbox="$REPO_ROOT/state/rooms/$chat_id/inbox.ndjson"
  local rlu="$REPO_ROOT/state/rooms/$chat_id/.rate_limited_until"
  local rlu_val="" rlu_note=""
  if [[ -f "$rlu" ]]; then
    rlu_val=$(cat "$rlu" 2>/dev/null)
    if [[ "$rlu_val" =~ ^[0-9]+$ ]] && (( $(date +%s) < rlu_val )); then
      rlu_note=" rate_limited_until=$rlu_val(FUTURE)"
    else
      rlu_note=" rate_limited_until=$rlu_val(past-or-invalid)"
    fi
  fi
  if [[ ! -f "$inbox" ]]; then
    [[ -n "${LOG:-}" ]] && echo "$(_diag_ts) reap_inbox_pipeline chat=$chat_id caller_pid=$$ result=no-inbox-file$rlu_note" >> "$LOG"
    return 0
  fi
  local inbox_re tails tp ppid pgid own_pgid live=0 killed=""
  inbox_re=$(printf '%s' "$inbox" | sed 's/[][\\.*^$()+?{|/]/\\&/g')
  tails=$(pgrep -f "tail -n[^|]* -f ${inbox_re}\$" 2>/dev/null || true)
  if [[ -z "$tails" ]]; then
    [[ -n "${LOG:-}" ]] && echo "$(_diag_ts) reap_inbox_pipeline chat=$chat_id caller_pid=$$ result=no-tail-found (safe to spawn)$rlu_note" >> "$LOG"
    return 0
  fi
  own_pgid=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
  for tp in $tails; do
    ppid=$(ps -o ppid= -p "$tp" 2>/dev/null | tr -d ' ')
    if [[ -z "$ppid" || "$ppid" == "1" ]]; then
      pgid=$(ps -o pgid= -p "$tp" 2>/dev/null | tr -d ' ')
      if [[ -n "$pgid" && "$pgid" != "$own_pgid" && "$pgid" != "1" ]]; then
        kill -9 -- "-$pgid" 2>/dev/null || true
        killed="$killed pgid:$pgid"
      else
        kill -9 "$tp" 2>/dev/null || true
        killed="$killed tail_pid:$tp"
      fi
    else
      live=1
      killed="$killed LIVE_SIBLING(tail_pid=$tp,wrapper_ppid=$ppid)"
    fi
  done
  [[ -n "${LOG:-}" ]] && echo "$(_diag_ts) reap_inbox_pipeline chat=$chat_id caller_pid=$$ tails=[$tails] live=$live acted=[$killed]$rlu_note" >> "$LOG"
  [[ "$live" == "0" ]]
}

# Newest mtime (epoch seconds) across this repo's own bash/python pipeline
# code. A persistent spawn reads its script text once at exec time, any
# edit made after that is invisible to it until it dies and restarts, which
# normally only happens via a crash, rate-limit, or manual /cancel. Compared
# against a spawn's own .spawn_started_at (spawn_claude.sh), this is how
# dispatch() tells "this room's live process is running stale code" instead
# of guessing at a fixed age cutoff, a spawn that's been up for a long time but
# started AFTER the last real code change is still current and must not be
# refreshed; one that's an hour old but started BEFORE a change made ten
# minutes ago is stale right now.
code_mtime() {
  find "$REPO_ROOT/scripts" "$REPO_ROOT/hooks" -type f \
    \( -name '*.sh' -o -name '*.py' \) -exec stat -f '%m' {} \; 2>/dev/null \
    | sort -rn | head -1
}

# mtime of the claude binary a fresh spawn would exec (the versioned file
# ~/.local/bin/claude symlinks to, so it changes when `claude update`
# installs a new version). A persistent spawn keeps running whatever binary
# it exec'd at start, so after an update it can reject newly released model
# ids ("does not support this model; version X or newer is required") until
# it is recycled. The listener's stale-spawn check folds this into its
# comparison so an update refreshes rooms the same way a script edit does.
claude_bin_mtime() {
  stat -L -f '%m' "$(claude_bin)" 2>/dev/null
}

# Short git commit sha the caller's code is currently running from, the
# human-meaningful "version" shown on the dashboard's per-room breakdown
# (which chats are being served by which commit, e.g. after a rollout).
# Falls back to "unknown" outside a git checkout rather than failing.
code_version() {
  git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown"
}

# Append one structured event to state/metrics.ndjson, the shared feed
# scripts/dashboard.py reads. Bash side of the metrics pipeline; the
# python side (stream_to_telegram.py) writes the identical line shape
# via metrics.py's emit() so both sides produce ONE consistent schema.
#
# Usage: emit_metric <event> [key=value ...]
#   emit_metric turn_end room=-123 duration_ms=4200 is_error=0
#
# Deliberately bash string-building, not a python subprocess per call,
# code_mtime's own multi-fork cost (see maybe_auto_restart in listener.sh)
# is the exact lesson: this runs from hot paths (every dispatched message,
# every turn), so forking an interpreter per event would reintroduce the
# same class of overhead.
# Values are escaped for JSON (backslash, quote, control chars) since
# room labels/error text can contain arbitrary characters; keys are
# trusted literals (always a fixed string at the call site, never user
# input), so they're inlined unescaped like the rest of this file's
# hand-built JSON (_park_room_for_rate_limit and friends do the same).
# Never blocks and never raises: a metrics write failing must not affect
# the bot's actual behaviour, so errors are swallowed.
emit_metric() {
  local event="$1"; shift
  local ts; ts=$(date -u +%FT%TZ)
  local json="{\"ts\":\"$ts\",\"event\":\"$(_json_escape "$event")\""
  local kv key val
  for kv in "$@"; do
    key="${kv%%=*}"
    val="${kv#*=}"
    json+=",\"$key\":\"$(_json_escape "$val")\""
  done
  json+="}"
  echo "$json" >> "$REPO_ROOT/state/metrics.ndjson" 2>/dev/null || true
}

# Global (not per-room) rate-limit alerts-channel dedup. Every room shares
# the same Anthropic account and therefore the same rate-limit windows,
# several rooms doing subagent-heavy parallel work routinely all trip the
# SAME shared limit within moments of each other, each independently
# pinging the alerts channel about what is really one account-wide event.
# Previously each room tracked its own
# state/rooms/<chat>/.rl_alerted, so N simultaneously-affected rooms sent
# N alerts for one shared limit.
#
# Dedup key is (resume_at, limit_type), stored globally at
# state/.rl_alerted: a genuinely different limit (a different type, or
# the same type resetting at a different epoch) still alerts fresh; the
# exact same limit window hit by a second or third room within
# RL_ALERT_DEDUP_S (setting, default 7200s, matching the old per-room window)
# alerts only once, no matter which room got there first.
#
# Usage: if should_alert_rate_limit <resume_at> <limit_type>; then ...
# Returns 0 (alert) or 1 (already alerted for this exact window, skip).
# Best-effort: any failure to read/write the marker fails toward alerting
# (never silently swallows a real hit just because the marker is stale).
should_alert_rate_limit() {
  local resume_at="$1" limit_type="$2"
  local f="$REPO_ROOT/state/.rl_alerted"
  local prev_resume="" prev_type="" prev_ts=0
  if [[ -f "$f" ]]; then
    IFS=$'\t' read -r prev_resume prev_type prev_ts < "$f" 2>/dev/null || true
  fi
  local now; now=$(date +%s)
  if [[ "$prev_resume" == "$resume_at" && "$prev_type" == "$limit_type" ]] \
     && [[ "$prev_ts" =~ ^[0-9]+$ ]] \
     && (( now - prev_ts < $(bot_config RL_ALERT_DEDUP_S 7200) )); then
    return 1
  fi
  printf '%s\t%s\t%s\n' "$resume_at" "$limit_type" "$now" > "$f" 2>/dev/null || true
  return 0
}

# JSON-escape a single value for use inside a hand-built "..." string.
# Order matters: backslash first (so later substitutions don't double-
# escape the backslashes they themselves introduce), then the other
# control characters. macOS bash 3.2 has no \xHH substitution, so
# literal control chars are matched via ANSI-C-quoted patterns instead.
_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

# Tear down the in-flight spawn for a room cleanly.
#
# Usage: kill_room_spawn <room_dir> [paint_cancel]
#
#   paint_cancel=1 → user-initiated kill (/new, /cd, /cancel). Touches
#                    `.cancel_requested` (streamer paints "🛑 cancelled")
#                    AND `.user_cancel` (wrapper preserves session_id on
#                    exit instead of treating the non-zero exit as crash).
#   paint_cancel=0 → admin teardown (/forget on another room). Neither
#                    marker; the wrapper's session-reset treats it as a
#                    real crash, which is correct for a removed room.
#   paint_cancel=stale → automatic refresh of a spawn whose running code
#                    predates an on-disk script change (dispatch()'s
#                    stale-spawn check). No visible badge, this should be
#                    invisible to the user, just a fresh process handling
#                    their next message. Touches `.stale_refresh`, which
#                    the wrapper treats like `.user_cancel` for session
#                    preservation, but logs distinctly and does NOT stamp
#                    `.crash_times`, it isn't a real crash and must not
#                    count toward the crash-loop circuit breaker.
#
# Bounded by a short sleep + KILL escalation; never blocks for >1.5s.
kill_room_spawn() {
  local rd="$1"
  local paint_cancel="${2:-0}"
  local lock="$rd/busy.lock"
  if [[ ! -f "$lock" ]]; then
    return 0
  fi
  local pid
  pid=$(cat "$lock" 2>/dev/null)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    if [[ "$paint_cancel" == "1" ]]; then
      touch "$rd/.cancel_requested"   # streamer's badge marker
      touch "$rd/.user_cancel"        # wrapper's session-preserve marker
      # A user-ordered stop also ends any /loop: the listener's missed-
      # wakeup rescue must not bring it back on the next spawn.
      rm -f "$rd/.wakeup_due"
    elif [[ "$paint_cancel" == "stale" ]]; then
      touch "$rd/.stale_refresh"
    fi
    kill_tree "$pid" TERM
    sleep 0.5
    kill_tree "$pid" KILL
  fi
  rm -f "$lock" "$rd/.cancel_requested" "$rd/.inbox_ready"
  : > "$rd/inbox.ndjson"
  # Keep .pending_reacts (see inject_to_inbox) 1:1 with inbox.ndjson: every
  # truncation of one must truncate the other, or a future turn's reaction
  # update could misfire onto a stale, unrelated message.
  : > "$rd/.pending_reacts"
}

# Reply with a text + inline keyboard. If the caller was invoked from a
# button tap (CALLBACK_MSG_ID set), edit that message in place; otherwise
# send a new message.
#
# Usage: reply_kb <text> <keyboard_json> [with_back]
#
#   with_back defaults to 1, appends a "⬅ Menu" row so every sub-screen
#   is one tap from the main palette. Pass 0 ONLY for the main menu screen
#   itself (a back-to-self button would be useless).
#
# Requires: ROOM_CHAT_ID set, REPO_ROOT set.
reply_kb() {
  local text="$1"
  local kb="$2"
  local with_back="${3:-1}"
  if [[ "$with_back" == "1" ]]; then
    # Strip the trailing ']' of the outer array, append a back-to-menu row.
    # Python validates so a malformed kb fails loudly here instead of
    # producing a busted 400 from Telegram.
    kb=$(KB="$kb" python3 <<'PYEOF'
import json, os, sys
try:
    rows = json.loads(os.environ['KB'])
    rows.append([{"text": "⬅ Menu", "callback_data": "act:menu"}])
    print(json.dumps(rows), end='')
except Exception as e:
    sys.stderr.write(f"reply_kb: bad keyboard json: {e}\n")
    print(os.environ['KB'], end='')  # fall through with original
PYEOF
)
  fi
  if [[ -n "${CALLBACK_MSG_ID:-}" ]]; then
    CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" editkb "$CALLBACK_MSG_ID" "$text" "$kb" >/dev/null
  else
    CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" kb "$text" "$kb" >/dev/null
  fi
}

# Post-restart "I'm back" announce. The menu's ♻️ restart action writes
# $REPO_ROOT/state/.restart_announce containing the chat_id that requested the
# restart; the freshly-booted listener calls this once at startup to tell that
# room the reload finished (otherwise the user just sees "Restarting…" and has
# to guess whether it came back).
#
# Single-use: the marker is ALWAYS removed, so it fires at most once.
# Time-bounded: a marker older than the window is dropped silently, so a failed
# kickstart can't fire a spurious "back up" on an unrelated crash-recovery boot
# hours later. Requires $REPO_ROOT set; safe to call when no marker exists.
announce_restart_if_marked() {
  local marker="$REPO_ROOT/state/.restart_announce"
  [[ -f "$marker" ]] || return 0
  local chat age
  chat=$(tr -d '\n\r ' < "$marker" 2>/dev/null)
  age=$(( $(date +%s) - $(stat -f %m "$marker" 2>/dev/null || echo 0) ))
  rm -f "$marker"
  [[ "$age" -lt 120 ]] || return 0
  is_valid_chat_id "$chat" || return 0
  CHAT_ID="$chat" "$REPO_ROOT/scripts/tg.sh" send \
    "✅ Back up and running on the latest code." >/dev/null 2>&1 || true
}

# Does <text> @-mention the bot <botuser>, as a whole token? Used by the group
# gate: in a group the bot only acts on messages that @ it. Whole-token + case-
# insensitive (Telegram usernames are case-insensitive), so "@mybot" matches but
# "@mybot_two" (a longer handle that merely starts with it) does NOT.
#
# Usage: mentions_bot "<text>" "<botuser>"   (botuser must be non-empty)
mentions_bot() {
  printf '%s' "$1" | grep -qiE "@${2}([^[:alnum:]_]|$)"
}
