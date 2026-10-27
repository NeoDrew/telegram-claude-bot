#!/bin/bash
# register_commands.sh: push the bot's slash-command list to Telegram via
# setMyCommands. Once registered, Telegram clients show a tappable
# autocomplete dropdown when the user types "/" in any chat with the bot.
#
# Idempotent: safe to re-run after adding/renaming commands. Telegram
# replaces the full list each call.
#
# Run after editing handle_action.sh's case statement:
#   ./scripts/register_commands.sh
#
# Commands list is defined inline below. Keep descriptions terse; Telegram
# truncates around 60-80 chars on mobile.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO_ROOT/scripts/lib.sh"
TOKEN=$(extract_var TELEGRAM_BOT_TOKEN)
if [[ -z "$TOKEN" ]]; then
  echo "register_commands.sh: TELEGRAM_BOT_TOKEN missing in .env" >&2
  exit 1
fi
# Overridable so tests/e2e/ can point this at a fake Bot API server.
TELEGRAM_API_BASE="$(bot_config TELEGRAM_API_BASE https://api.telegram.org)"

# Menu-first UX: only ONE slash command is exposed (/menu). Everything
# else lives behind buttons in the menu's inline keyboard, fired as
# "act:<cmd>" callback_data. Slash autocomplete loses to one-tap menus
# on a phone. Only the typed /menu and /start reach the bot's own
# dispatcher; any other typed slash command is passed to Claude as plain
# text (so Claude Code's own slash commands and skills still work).
# /start is the Telegram-standard welcome trigger and is
# aliased to the menu action in handle_action.sh, so opening a fresh
# chat with the bot lands on the menu immediately.
read -r -d '' COMMANDS <<'EOF' || true
[
  {"command":"menu", "description":"Open the menu (every action as a button)"}
]
EOF

# Clear any scope-specific overrides before setting the default. Telegram's
# scope resolution picks the MOST specific scope first: if a previous run
# (or a one-off test call) ever set commands for `all_group_chats` or
# `all_private_chats` (even to an empty list), that override wins over
# the default and silences slash autocomplete in those chats (a forgotten
# test call can leave groups showing no commands); deleting both
# overrides on every run forces a clean fall-through to the
# default scope.
for scope_type in all_group_chats all_private_chats; do
  echo "Clearing setMyCommands override for scope=${scope_type}…"
  curl -s --max-time 10 -X POST \
    "${TELEGRAM_API_BASE}/bot${TOKEN}/deleteMyCommands" \
    -H "Content-Type: application/json" \
    -d "{\"scope\":{\"type\":\"${scope_type}\"}}" >/dev/null
done

echo "Registering ${#COMMANDS} bytes of commands with Telegram (default scope)…"
RESP=$(curl -s --max-time 15 -X POST \
  "${TELEGRAM_API_BASE}/bot${TOKEN}/setMyCommands" \
  -H "Content-Type: application/json" \
  -d "{\"commands\":${COMMANDS}}")

echo "Response: $RESP"
echo "$RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('ok'):
    print('✓ Commands registered. Telegram clients will pick up the autocomplete within ~60s.')
else:
    print(f'✗ FAILED: {d.get(\"description\", \"unknown error\")}')
    sys.exit(1)
"

# Set the Menu button next to the chat input to type=commands. That makes
# Telegram show a persistent "Menu" button beside the message bar; tapping
# it pops open the autocomplete list. More discoverable than typing "/".
echo
echo "Setting chat menu button to type=commands…"
MENU_RESP=$(curl -s --max-time 10 -X POST \
  "${TELEGRAM_API_BASE}/bot${TOKEN}/setChatMenuButton" \
  -H "Content-Type: application/json" \
  -d '{"menu_button":{"type":"commands"}}')
echo "$MENU_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('ok'):
    print('✓ Chat menu button set to commands.')
else:
    print(f'✗ FAILED: {d.get(\"description\", \"unknown error\")}')
"
