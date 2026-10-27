#!/bin/bash
# Stop hook: push an alert when a Claude Code session ends on this machine.
# Skipped when CLAUDE_HOOKS_SUPPRESS_TELEGRAM=1 (bot-spawned sessions
# already handle their own notifications; this is for terminal/IDE sessions).

[[ "${CLAUDE_HOOKS_SUPPRESS_TELEGRAM:-}" == "1" ]] && exit 0

# Read the hook payload from stdin (JSON with session_id, cwd, etc.)
PAYLOAD=$(cat)
CWD=$(echo "$PAYLOAD" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || true)

if [[ -n "$CWD" ]]; then
  LABEL=$(basename "$CWD")
  MSG="Claude session finished in $LABEL"
else
  MSG="Claude session finished"
fi

exec "${NOTIFY_CMD:-$HOME/.local/bin/notify-cmd}" --alert --tag "[claude]" "$MSG"
