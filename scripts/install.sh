#!/bin/bash
# install.sh - install or reinstall the launchd job for the central bot.
#
# Idempotent: safe to run multiple times. Will unload existing job if present,
# rewrite the plist, reload.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO_ROOT/scripts/lib.sh"
LABEL="$(bot_label)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LISTENER="$REPO_ROOT/scripts/listener.sh"

if [[ ! -x "$LISTENER" ]]; then
  echo "install.sh: $LISTENER missing or not executable" >&2
  exit 1
fi

# Make sure state + log dirs exist
mkdir -p "$REPO_ROOT/state" "$REPO_ROOT/logs"

# Create the venv used by /tab for window enumeration / capture via PyObjC.
# If the venv already exists, leave it alone (assume it has the right deps).
VENV="$REPO_ROOT/.venv"
if [[ ! -x "$VENV/bin/python3" ]]; then
  PY=$(bot_python)
  if [[ -n "$PY" ]]; then
    echo "Creating .venv at $VENV (for /tab)…"
    "$PY" -m venv "$VENV" || echo "venv creation failed (skip /tab support)"
    if [[ -x "$VENV/bin/pip" ]]; then
      "$VENV/bin/pip" install --quiet pyobjc-framework-Quartz \
        || echo "pyobjc-framework-Quartz install failed (/tab will be unavailable)"
    fi
  else
    echo "No python3 found; skipping .venv creation. /tab will be unavailable."
  fi
fi

# Put notify-cmd on PATH (~/.local/bin) as a link to the repo copy, so any
# session or cron job can push to the phone. Never replaces a real file.
NOTIFY_LINK="$HOME/.local/bin/notify-cmd"
mkdir -p "$HOME/.local/bin"
if [[ -L "$NOTIFY_LINK" || ! -e "$NOTIFY_LINK" ]]; then
  ln -sfn "$REPO_ROOT/scripts/notify-cmd" "$NOTIFY_LINK"
else
  echo "Leaving existing $NOTIFY_LINK alone (not a link to this repo)."
fi

# Locate the node bin dir (claude's plugin hooks need it on PATH).
NODE_BIN_DIR=$(dirname "$(command -v node 2>/dev/null || true)")
if [[ -z "$NODE_BIN_DIR" || "$NODE_BIN_DIR" == "." ]]; then
  # launchd-style minimal PATH: fall back to the newest nvm install.
  NODE_BIN_DIR=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${LISTENER}</string>
  </array>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${NODE_BIN_DIR}:${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin</string>
    <key>HOME</key><string>${HOME}</string>
  </dict>

  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>

  <key>StandardOutPath</key>
  <string>${REPO_ROOT}/logs/launchd.out</string>
  <key>StandardErrorPath</key>
  <string>${REPO_ROOT}/logs/launchd.err</string>
</dict>
</plist>
PLIST

if ! plutil -lint "$PLIST" >/dev/null; then
  echo "install.sh: generated plist invalid" >&2
  exit 2
fi

# Reload
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST" || { echo "install.sh: launchctl load failed"; exit 3; }

sleep 2
PID=$(launchctl list | awk -v label="$LABEL" '$3 == label {print $1}')
echo "Installed and loaded ${LABEL}"
echo "PID: ${PID:-(not yet running)}"
echo "Plist: $PLIST"
echo "Tail logs with: tail -f ${REPO_ROOT}/logs/listener.log"

# Register the bot's slash-command list with Telegram so clients show an
# autocomplete dropdown when the user types "/". Safe to re-run.
if [[ -x "$REPO_ROOT/scripts/register_commands.sh" ]]; then
  echo "Registering slash-command autocomplete with Telegram…"
  "$REPO_ROOT/scripts/register_commands.sh" || echo "(register_commands.sh failed; not fatal, the bot still works)"
fi
