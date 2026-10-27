#!/bin/bash
# install_dashboard.sh - install or reinstall the launchd job for the
# metrics dashboard (scripts/dashboard.py), separate from the main bot's
# own listener job so the two can be started/stopped/restarted
# independently.
#
# Idempotent: safe to run multiple times. Will unload the existing job if
# present, rewrite the plist, reload.
#
# The dashboard binds 127.0.0.1 ONLY (see dashboard.py's own docstring);
# it is never reachable from outside this machine. Nothing here changes that.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO_ROOT/scripts/lib.sh"
LABEL="$(bot_label)-dashboard"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DASHBOARD="$REPO_ROOT/scripts/dashboard.py"
PORT="$(bot_config BOT_DASHBOARD_PORT 8787)"

if [[ ! -f "$DASHBOARD" ]]; then
  echo "install_dashboard.sh: $DASHBOARD missing" >&2
  exit 1
fi

PYTHON3=$(command -v python3 || true)
if [[ -z "$PYTHON3" ]]; then
  echo "install_dashboard.sh: python3 not found on PATH" >&2
  exit 1
fi

mkdir -p "$REPO_ROOT/state" "$REPO_ROOT/logs"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON3}</string>
    <string>${DASHBOARD}</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>

  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>

  <key>StandardOutPath</key>
  <string>${REPO_ROOT}/logs/dashboard.out</string>
  <key>StandardErrorPath</key>
  <string>${REPO_ROOT}/logs/dashboard.err</string>
</dict>
</plist>
PLIST

if ! plutil -lint "$PLIST" >/dev/null; then
  echo "install_dashboard.sh: generated plist invalid" >&2
  exit 2
fi

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST" || { echo "install_dashboard.sh: launchctl load failed"; exit 3; }

sleep 1
PID=$(launchctl list | awk -v label="$LABEL" '$3 == label {print $1}')
echo "Installed and loaded ${LABEL}"
echo "PID: ${PID:-(not yet running)}"
echo "Plist: $PLIST"
echo "Dashboard: http://127.0.0.1:${PORT}/"
echo "Tail logs with: tail -f ${REPO_ROOT}/logs/dashboard.err"
