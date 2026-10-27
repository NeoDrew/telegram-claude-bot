#!/bin/bash
# safe_restart.sh: restart the listener after showing what's running.
#
# With the setsid detach in place (see scripts/with_setsid.py + listener.sh),
# `launchctl kickstart -k` only kills the listener process itself; spawns
# keep running in their own process groups, the new listener picks up
# Telegram polling, and spawns continue streaming to Telegram. So a
# "safe" restart is mostly informational, but seeing the active rooms
# before kicking is still useful (e.g. confirms which conversations
# you're leaving alive vs. which you should /cancel first).
#
# Usage:
#   scripts/safe_restart.sh              # interactive: list + confirm + kick
#   scripts/safe_restart.sh --force      # skip confirmation, just kick
#   scripts/safe_restart.sh --list       # list only, do not restart

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO_ROOT/scripts/lib.sh"

JOB_LABEL="$(bot_label)"
MODE="${1:-interactive}"

# Resolve current listener pid via launchctl (single source of truth;
# pgrep'ing the script can match stale shells or recently-killed ones).
LISTENER_PID=$(launchctl list | awk -v label="$JOB_LABEL" '$3 == label {print $1}')
if [[ -z "$LISTENER_PID" || "$LISTENER_PID" == "-" ]]; then
  echo "listener not running (launchd has no pid for $JOB_LABEL)"
  LISTENER_PID=""
fi

# Header.
echo "=== listener ==="
if [[ -n "$LISTENER_PID" ]]; then
  ps -p "$LISTENER_PID" -o pid,pgid,etime,command 2>/dev/null | head -2
else
  echo "(not running; kicking will start it cold)"
fi
echo

# Per-room active state. is_busy is the canonical "is the spawn alive"
# check (see lib.sh, pid in busy.lock + tail -f signal). Render one line
# per room with label, busy state, session id short, etime if busy.
echo "=== rooms ==="
for rd in "$REPO_ROOT"/state/rooms/*/; do
  [[ -d "$rd" ]] || continue
  chat_id=$(basename "$rd")
  label=$(cat "$rd/label" 2>/dev/null || echo "(no label)")
  if is_busy "$chat_id"; then
    pid=$(cat "$rd/busy.lock" 2>/dev/null)
    etime=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ' || echo "?")
    pgid=$(ps -p "$pid" -o pgid= 2>/dev/null | tr -d ' ' || echo "?")
    # If the spawn's pgid matches the listener's, it will DIE with the
    # listener (pre-setsid spawn). Flag it so the user knows.
    if [[ -n "$LISTENER_PID" && "$pgid" == "$LISTENER_PID" ]]; then
      echo "  🔴 BUSY  $label ($chat_id) pid=$pid etime=$etime"
      echo "       ⚠️  same pgid as listener; WILL DIE on restart"
    else
      echo "  🟠 BUSY  $label ($chat_id) pid=$pid pgid=$pgid etime=$etime"
      echo "       ✓ detached pgid; survives restart"
    fi
  else
    echo "  💤 idle  $label ($chat_id)"
  fi
done
echo

case "$MODE" in
  --list)
    exit 0
    ;;
  --force)
    echo "→ launchctl kickstart -k gui/$UID/$JOB_LABEL"
    launchctl kickstart -k "gui/$UID/$JOB_LABEL"
    ;;
  interactive|*)
    read -r -p "Restart now? [y/N] " ans
    if [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]; then
      echo "→ launchctl kickstart -k gui/$UID/$JOB_LABEL"
      launchctl kickstart -k "gui/$UID/$JOB_LABEL"
    else
      echo "aborted"
      exit 1
    fi
    ;;
esac

# Brief verification: was the listener restarted?
sleep 1
NEW_PID=$(launchctl list | awk -v label="$JOB_LABEL" '$3 == label {print $1}')
if [[ -n "$NEW_PID" && "$NEW_PID" != "-" && "$NEW_PID" != "$LISTENER_PID" ]]; then
  echo "✓ new listener pid: $NEW_PID"
else
  echo "⚠️  listener pid unchanged or unavailable; check 'launchctl list | grep $JOB_LABEL'"
fi
