#!/bin/bash
# bash-guard.sh: PreToolUse hook that blocks Bash commands targeting the
# telegram-claude-bot infrastructure. Runs before every Bash tool use.
#
# Claude Code passes the tool input as JSON on stdin:
#   {"command": "...", "description": "..."}
#
# Exit 0 = allow. Exit non-zero = block (stdout becomes the error message).

set -uo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('command', '') or d.get('cmd', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Patterns that would damage the bot's own infrastructure.
# Claude must never kill or restart the listener from inside a spawn.
BLOCKED_PATTERNS=(
  "pkill.*listener"
  "kill.*listener"
  "killall.*listener"
  "pkill.*spawn_claude"
  "kill.*spawn_claude"
  "launchctl.*telegram"
  "launchctl.*claude.bot"
  "pkill.*telegram-claude"
  "kill.*telegram-claude"
  "rm.*busy\.lock"
  "rm.*sessions\.json"
)

for pattern in "${BLOCKED_PATTERNS[@]}"; do
  if echo "$COMMAND" | grep -qiE "$pattern"; then
    echo "🚫 BLOCKED by bash-guard: command matches protected pattern '$pattern'."
    echo "You are running AS the telegram-claude-bot. Never kill or restart the listener or related processes from within a spawn."
    echo "If you need to fix bot infrastructure issues, report them to the user; don't attempt to fix them from here."
    exit 1
  fi
done

exit 0
