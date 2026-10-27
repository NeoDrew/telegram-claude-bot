#!/bin/bash
# handle_action.sh - dispatch a single action invocation to its handler.
# Outputs the reply text to stdout so the listener can pipe it to tg.sh send.
#
# Invocation (called by listener.sh):
#     handle_action.sh <cmd> [args...]
#
#   <cmd>   bare verb, NO leading slash (e.g. "menu", "effort", "cancel")
#   args... rest of the line, joined with single spaces
#
# Why no slashes here any more: the bot is menu-driven. Only the typed
# /menu and /start reach this dispatcher (both route to the "menu" action);
# any other typed text, slash or not, goes to Claude as plain text, so
# Claude Code's own slash commands still work. Every other entry point is an inline-keyboard
# button whose callback_data is "act:<cmd>[ args]". The "act:" prefix is
# stripped in the listener and the remainder lands here. Keeping the wire
# format slash-free means the dispatcher and the per-command handlers can
# stop pretending they're parsing slash commands when they're not.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Multi-chat: each Telegram chat the bot is in (DM, group, topic) is a "room"
# with its own session, queue, busy-lock, effort and scheduled tasks.
# Listener exports CHAT_ID for routing; we fall back to the .env primary
# chat when called directly from a shell.
. "$REPO_ROOT/scripts/lib.sh"
# Resolved once so every handler (and its inline Python) sees the same value.
export BOT_PROJECTS_DIR="$(projects_dir)"
ROOM_CHAT_ID="${CHAT_ID:-$(extract_var TELEGRAM_CHAT_ID)}"
# Refuse to materialise state for obviously-fake chat ids. Without this,
# a stray manual invocation (CHAT_ID=123456 bash handle_action.sh ...) or
# a test that leaks its env value into the real repo creates a ghost
# state/rooms/<bogus>/ that then shows up in /rooms forever. See lib.sh
# for the policy (abs >= 1,000,000, strict integer).
if ! is_valid_chat_id "$ROOM_CHAT_ID"; then
  echo "handle_action.sh: refused to materialise state for invalid chat_id='${ROOM_CHAT_ID}'" >&2
  exit 1
fi
ROOM_DIR="$REPO_ROOT/state/rooms/$ROOM_CHAT_ID"
mkdir -p "$ROOM_DIR"
SESSIONS_FILE="$ROOM_DIR/sessions.json"
BUSY_LOCK="$ROOM_DIR/busy.lock"
QUEUE_FILE="$ROOM_DIR/queue.jsonl"
EFFORT_FILE="$ROOM_DIR/effort"
MODEL_FILE="$ROOM_DIR/model"
SCHED_FILE="$ROOM_DIR/scheduled.json"
# Global scheduled.json: one file, all rooms. Each task carries its own
# target_chat_id. SCHED_FILE above is a legacy variable some command handler
# headers still list as "in scope" for back-compat, but nothing reads or
# writes it (no command handler touches it, no test references it). It's
# intentionally NOT auto-created any more; the live system uses
# GLOBAL_SCHED_FILE exclusively.
GLOBAL_SCHED_FILE="$REPO_ROOT/state/scheduled.json"
[[ -f "$GLOBAL_SCHED_FILE" ]] || echo "[]" > "$GLOBAL_SCHED_FILE"

# Two call shapes are accepted because callers differ:
#   handle_action.sh "cd" "myapp"   → CMD=cd, ARGS=myapp (listener route)
#   handle_action.sh "cd myapp"     → CMD=cd, ARGS=myapp (single-string,
#                                       what the listener uses after stripping
#                                       the "act:" prefix from a callback)
if [[ $# -ge 2 ]]; then
  CMD_RAW="$1"
  shift
  ARGS="$*"
else
  CMD_LINE="${1:-}"
  CMD_RAW=$(echo "$CMD_LINE" | awk '{print $1}')
  ARGS=$(echo "$CMD_LINE" | cut -d' ' -f2-)
  if [[ "$ARGS" == "$CMD_RAW" ]]; then ARGS=""; fi
fi
# Defensive: strip a leading "/" so old call sites that still pass slash
# commands keep working during the migration window. Case-fold the verb so
# "Effort" / "EFFORT" / "effort" all match.
CMD_RAW="${CMD_RAW#/}"
# Telegram appends @botname to slash commands in groups ("/menu@your_claude_bot").
# Drop any @suffix on the verb so the listener can pass typed commands through
# without pre-stripping.
CMD_RAW="${CMD_RAW%%@*}"
CMD=$(echo "$CMD_RAW" | awk '{print tolower($0)}')

# Init room state if missing. room_state.py owns the sessions.json schema:
# {"active_cwd": "<absolute path>", "session_id": "<uuid or empty>"}.
# A room has at most ONE session at a time. The "cd" action to a different
# cwd drops the session entirely (no per-cwd resume map).
[[ -f "$SESSIONS_FILE" ]] || python3 "$REPO_ROOT/scripts/room_state.py" init "$SESSIONS_FILE"
[[ -f "$QUEUE_FILE" ]] || : > "$QUEUE_FILE"
[[ -f "$EFFORT_FILE" ]] || : > "$EFFORT_FILE"

# kill_room_spawn / kill_tree / reply_kb / extract_var / is_busy live in lib.sh.

case "$CMD" in
  new)                . "$REPO_ROOT/scripts/commands/new.sh" ;;
  cd)                 . "$REPO_ROOT/scripts/commands/cd.sh" ;;
  cdbrowse)           . "$REPO_ROOT/scripts/commands/cdbrowse.sh" ;;
  status)             . "$REPO_ROOT/scripts/commands/status.sh" ;;
  windows|screenshot) . "$REPO_ROOT/scripts/commands/windows.sh" ;;
  tab)                . "$REPO_ROOT/scripts/commands/tab.sh" ;;
  schedule)           . "$REPO_ROOT/scripts/commands/schedule.sh" ;;
  effort)             . "$REPO_ROOT/scripts/commands/effort.sh" ;;
  model)              . "$REPO_ROOT/scripts/commands/model.sh" ;;
  verbosity)          . "$REPO_ROOT/scripts/commands/verbosity.sh" ;;
  plugin)             . "$REPO_ROOT/scripts/commands/plugin.sh" ;;
  cancel)             . "$REPO_ROOT/scripts/commands/cancel.sh" ;;
  compact)            . "$REPO_ROOT/scripts/commands/compact.sh" ;;
  compact_new)        . "$REPO_ROOT/scripts/commands/compact_new.sh" ;;
  autocompact)        . "$REPO_ROOT/scripts/commands/autocompact.sh" ;;
  usage)              . "$REPO_ROOT/scripts/commands/usage.sh" ;;
  rooms)              . "$REPO_ROOT/scripts/commands/rooms.sh" ;;
  users)              . "$REPO_ROOT/scripts/commands/users.sh" ;;
  login)              . "$REPO_ROOT/scripts/commands/login.sh" ;;
  noop)               . "$REPO_ROOT/scripts/commands/noop.sh" ;;
  close)              . "$REPO_ROOT/scripts/commands/close.sh" ;;
  close_delete)       . "$REPO_ROOT/scripts/commands/close_delete.sh" ;;
  restart)            . "$REPO_ROOT/scripts/commands/restart.sh" ;;
  mentions)           . "$REPO_ROOT/scripts/commands/mentions.sh" ;;
  alerts)             . "$REPO_ROOT/scripts/commands/alerts.sh" ;;
  tune)               . "$REPO_ROOT/scripts/commands/tune.sh" ;;
  more)               . "$REPO_ROOT/scripts/commands/more.sh" ;;
  forget)             . "$REPO_ROOT/scripts/commands/forget.sh" ;;
  label)              . "$REPO_ROOT/scripts/commands/label.sh" ;;
  ask_answer)         . "$REPO_ROOT/scripts/commands/ask_answer.sh" ;;
  menu|start)         . "$REPO_ROOT/scripts/commands/menu.sh" ;;
  menu_new)           . "$REPO_ROOT/scripts/commands/menu_new.sh" ;;
  help)               . "$REPO_ROOT/scripts/commands/help.sh" ;;

  *)
    echo "Unknown action: $CMD."
    ;;
esac
