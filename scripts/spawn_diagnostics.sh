#!/bin/bash
# spawn_diagnostics.sh - forensic sidecar for a single persistent-mode spawn.
#
# Started in background by spawn_claude.sh. Polls every 30s and records a
# snapshot of every piece of state we'd want when investigating a future
# "the stream froze and claude hung forever" bug:
#
#   - claude.stream.jsonl mtime/size  (did claude emit anything since last tick?)
#   - claude process state             (sleeping? running? %cpu? rss?)
#   - claude TCP connections           (count, only the api.anthropic.com ones)
#   - api.anthropic.com reachability   (single TCP-port ping latency)
#   - DNS resolution time              (dig api.anthropic.com)
#   - Wi-Fi state, when present         (airport -I: RSSI, channel, state)
#
# Output: one NDJSON line per snapshot to logs/rooms/<chat>/spawn_diag.jsonl
# (truncated per spawn, like the audit log).
#
# Stall detection: if stream_log mtime hasn't moved in STALL_THRESHOLD_S (60s)
# AND claude is still alive AND its TCP state suggests an in-flight request
# (an ESTABLISHED conn to api.anthropic.com), we dump deep forensics to
# logs/rooms/<chat>/stall_<ts>.txt: TCP states, full lsof, sample claude.
# These files are the smoking gun for "is this RST, FIN, or just silence?"
#
# Usage (caller fills in):
#   spawn_diagnostics.sh <spawn_pid> <stream_log> <diag_dir>
#
# Exit: SIGTERM from the caller's trap. We tidy nothing on exit; logs stay.

set -uo pipefail

SPAWN_PID="${1:?spawn_pid required}"
STREAM_LOG="${2:?stream log path required}"
DIAG_DIR="${3:?diag dir required}"

mkdir -p "$DIAG_DIR"
DIAG_LOG="$DIAG_DIR/spawn_diag.jsonl"
: > "$DIAG_LOG"

POLL_INTERVAL_S=30
STALL_THRESHOLD_S=60
AIRPORT="/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"

# Find the claude PID. It's a child of the spawn wrapper. The pipeline
# (tail | claude | tee | streamer) takes a moment to fully fork after
# spawn_claude.sh launches it, empirically up to ~30s when the bot is
# under load, so we poll for up to 60s before giving up.
#
# Identification: walk children of SPAWN_PID and pick the one whose argv
# contains "claude" (the binary path) plus a session flag. BSD pgrep's -f
# regex syntax differs from GNU's (alternation is unreliable), so we do
# the matching in bash explicitly.
find_claude_pid() {
  local i tries=120  # 120 * 0.5s = 60s budget
  for ((i=0; i<tries; i++)); do
    local child
    for child in $(pgrep -P "$SPAWN_PID" 2>/dev/null); do
      local cmd
      cmd=$(ps -p "$child" -o command= 2>/dev/null)
      # Match the real claude process: it has the binary path AND one of
      # the session flags. Excludes the tail/tee/streamer children.
      if [[ "$cmd" == *claude* ]] && \
         { [[ "$cmd" == *--session-id* ]] || [[ "$cmd" == *--resume* ]]; }; then
        echo "$child"
        return 0
      fi
    done
    sleep 0.5
  done
  return 1
}

CLAUDE_PID=$(find_claude_pid || true)
if [[ -z "$CLAUDE_PID" ]]; then
  echo "{\"ts\":\"$(date -u +%FT%TZ)\",\"event\":\"diag_aborted\",\"reason\":\"claude_pid_not_found\"}" >> "$DIAG_LOG"
  exit 1
fi

# --- helpers -----------------------------------------------------------------

iso_ts() { date -u +%FT%T.%3NZ; }

# Snapshot the claude process. Returns a compact JSON fragment or null fields
# if the pid is gone. ps STAT field tells us if it's sleeping/running/zombie;
# wchan would be ideal but is usually unhelpful on macOS for stripped binaries.
proc_snapshot() {
  if ! kill -0 "$CLAUDE_PID" 2>/dev/null; then
    echo '"proc":null'
    return
  fi
  local row
  row=$(ps -p "$CLAUDE_PID" -o stat=,etime=,pcpu=,rss= 2>/dev/null | head -1)
  if [[ -z "$row" ]]; then
    echo '"proc":null'
    return
  fi
  local stat etime pcpu rss
  read -r stat etime pcpu rss <<< "$row"
  printf '"proc":{"stat":"%s","etime":"%s","pcpu":%s,"rss_kb":%s}' \
    "$stat" "$etime" "$pcpu" "$rss"
}

# Snapshot all TCP connections held by claude. We're specifically interested
# in api.anthropic.com (port 443): count + states. lsof's -nP avoids DNS
# lookups (fast, deterministic).
tcp_snapshot() {
  local out
  out=$(/usr/sbin/lsof -nP -a -p "$CLAUDE_PID" -iTCP 2>/dev/null)
  if [[ -z "$out" ]]; then
    echo '"tcp":{"total":0,"anthropic":[]}'
    return
  fi
  local total anth
  total=$(echo "$out" | tail -n +2 | wc -l | tr -d ' ')
  # Pull just the api.anthropic.com:443 connections, get their state in parens.
  # lsof emits "host:port->host2:port (STATE)" for the NAME field.
  anth=$(echo "$out" | tail -n +2 | grep -E "443" | awk '{
    state="UNKNOWN"
    for (i=NF; i>=1; i--) if ($i ~ /^\(.*\)$/) {state=$i; gsub(/[()]/, "", state); break}
    print state
  }' | sort | uniq -c | awk '{printf "\"%s\":%d,", $2, $1}' | sed 's/,$//')
  printf '"tcp":{"total":%d,"anthropic_443":{%s}}' "$total" "$anth"
}

# Stream log freshness: mtime + size. Compare to previous tick to detect
# stalls. We don't track this in shell across iterations cleanly, so caller
# diffs across the JSONL externally.
stream_snapshot() {
  if [[ -f "$STREAM_LOG" ]]; then
    local mtime size now age
    mtime=$(stat -f "%m" "$STREAM_LOG" 2>/dev/null || echo 0)
    size=$(stat -f "%z" "$STREAM_LOG" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$((now - mtime))
    printf '"stream":{"mtime":%s,"size":%s,"age_s":%s}' "$mtime" "$size" "$age"
  else
    echo '"stream":null'
  fi
}

# Reachability test: TCP connect to api.anthropic.com:443. We avoid HTTPS
# handshake (no token cost, no auth surface). nc -G is the connect timeout.
# Reports latency in ms, or -1 on failure.
reach_snapshot() {
  local t0 t1 ms ok=0
  t0=$(python3 -c 'import time; print(int(time.time()*1000))')
  if /usr/bin/nc -z -G 3 api.anthropic.com 443 2>/dev/null; then
    ok=1
  fi
  t1=$(python3 -c 'import time; print(int(time.time()*1000))')
  ms=$((t1 - t0))
  printf '"reach":{"ok":%s,"ms":%s}' "$ok" "$ms"
}

# DNS resolution time for api.anthropic.com. dig +stats gives us "Query time"
# but the wrapper-time is simpler and includes any caching slowdown.
dns_snapshot() {
  local t0 t1 ms ok=0 addr=""
  t0=$(python3 -c 'import time; print(int(time.time()*1000))')
  addr=$(dig +short +time=2 +tries=1 api.anthropic.com A 2>/dev/null | grep -E '^[0-9]' | head -1)
  if [[ -n "$addr" ]]; then ok=1; fi
  t1=$(python3 -c 'import time; print(int(time.time()*1000))')
  ms=$((t1 - t0))
  printf '"dns":{"ok":%s,"ms":%s,"addr":"%s"}' "$ok" "$ms" "$addr"
}

# Wifi state via airport. -I prints a key:value block. We just extract RSSI,
# noise, channel, state. If airport missing (ethernet, M-series with no
# Wi-Fi card visible) we report null.
wifi_snapshot() {
  if [[ ! -x "$AIRPORT" ]]; then
    echo '"wifi":null'
    return
  fi
  local out
  out=$("$AIRPORT" -I 2>/dev/null)
  if [[ -z "$out" ]]; then
    echo '"wifi":null'
    return
  fi
  local rssi noise channel state
  rssi=$(echo "$out" | awk -F': ' '/agrCtlRSSI:/ {print $2}' | tr -d ' ')
  noise=$(echo "$out" | awk -F': ' '/agrCtlNoise:/ {print $2}' | tr -d ' ')
  channel=$(echo "$out" | awk -F': ' '/^[[:space:]]*channel:/ {print $2}' | tr -d ' ')
  state=$(echo "$out" | awk -F': ' '/state:/ {print $2}' | tr -d ' ' | head -1)
  printf '"wifi":{"rssi":%s,"noise":%s,"channel":"%s","state":"%s"}' \
    "${rssi:-null}" "${noise:-null}" "$channel" "$state"
}

# When a stall is detected, write a focused forensics file. The diag JSONL
# is for the timeline view; this file is "everything I could grab at that
# moment" so a human can reconstruct.
dump_stall_forensics() {
  local stamp="$1"
  local out="$DIAG_DIR/stall_${stamp}.txt"
  {
    echo "==== STALL FORENSICS @ $(iso_ts) ===="
    echo "claude_pid=$CLAUDE_PID stream_log=$STREAM_LOG"
    echo
    echo "---- ps (claude + descendants) ----"
    ps -eo pid,ppid,etime,pcpu,rss,wchan,stat,command | awk -v p="$CLAUDE_PID" '
      NR==1 {print; next}
      $1==p {print; tracked[p]=1; next}
      $2 in tracked {print; tracked[$1]=1}
    '
    echo
    echo "---- lsof TCP (claude) ----"
    /usr/sbin/lsof -nP -a -p "$CLAUDE_PID" -iTCP 2>/dev/null
    echo
    echo "---- lsof FILES (claude, last 40 entries) ----"
    /usr/sbin/lsof -nP -p "$CLAUDE_PID" 2>/dev/null | tail -40
    echo
    echo "---- netstat ESTABLISHED + CLOSE_WAIT to 443 ----"
    netstat -an -p tcp 2>/dev/null | grep -E "\.443[[:space:]]" | head -40
    echo
    echo "---- sample claude (1s) ----"
    sample "$CLAUDE_PID" 1 -mayDie 2>/dev/null | head -80
    echo
    echo "---- last 5 lines of stream log ----"
    tail -5 "$STREAM_LOG" 2>/dev/null | cut -c1-400
    echo
    echo "---- ifconfig en0 (primary interface) ----"
    ifconfig en0 2>/dev/null | head -20
    echo
    if [[ -x "$AIRPORT" ]]; then
      echo "---- airport -I ----"
      "$AIRPORT" -I
      echo
    fi
    echo "==== END FORENSICS ===="
  } > "$out" 2>&1
  echo "$out"
}

# --- main poll loop ----------------------------------------------------------

echo "{\"ts\":\"$(iso_ts)\",\"event\":\"diag_started\",\"claude_pid\":$CLAUDE_PID,\"stream_log\":\"$STREAM_LOG\"}" >> "$DIAG_LOG"

prev_mtime=0
stalled_at=0   # epoch seconds when stall first observed; 0 = not stalled

while true; do
  # If claude died, log it and exit (caller's trap will tidy us anyway).
  if ! kill -0 "$CLAUDE_PID" 2>/dev/null; then
    echo "{\"ts\":\"$(iso_ts)\",\"event\":\"claude_gone\",\"claude_pid\":$CLAUDE_PID}" >> "$DIAG_LOG"
    exit 0
  fi

  proc=$(proc_snapshot)
  tcp=$(tcp_snapshot)
  stream=$(stream_snapshot)
  reach=$(reach_snapshot)
  dns=$(dns_snapshot)
  wifi=$(wifi_snapshot)

  printf '{"ts":"%s",%s,%s,%s,%s,%s,%s}\n' \
    "$(iso_ts)" "$proc" "$tcp" "$stream" "$reach" "$dns" "$wifi" >> "$DIAG_LOG"

  # Stall detection: stream log mtime hasn't moved AND we have at least one
  # ESTABLISHED connection to api.anthropic.com (otherwise it's just idle
  # between turns, not a stall).
  cur_mtime=$(stat -f "%m" "$STREAM_LOG" 2>/dev/null || echo 0)
  now=$(date +%s)
  age=$((now - cur_mtime))
  if (( age >= STALL_THRESHOLD_S )); then
    # Is claude actually holding a connection open? If yes → real stall.
    est_count=$(/usr/sbin/lsof -nP -a -p "$CLAUDE_PID" -iTCP 2>/dev/null | \
                  awk '/443/ && /ESTABLISHED/' | wc -l | tr -d ' ')
    if (( est_count > 0 )) && (( stalled_at == 0 )); then
      stamp=$(date -u +%Y%m%dT%H%M%SZ)
      forensic_path=$(dump_stall_forensics "$stamp")
      echo "{\"ts\":\"$(iso_ts)\",\"event\":\"stall_detected\",\"silent_s\":$age,\"est_conns\":$est_count,\"forensics\":\"$forensic_path\"}" >> "$DIAG_LOG"
      stalled_at=$now
    fi
  else
    # Stream moved again → end of stall (or never started one).
    if (( stalled_at != 0 )); then
      echo "{\"ts\":\"$(iso_ts)\",\"event\":\"stall_cleared\",\"duration_s\":$((now - stalled_at))}" >> "$DIAG_LOG"
      stalled_at=0
    fi
  fi
  prev_mtime=$cur_mtime

  sleep "$POLL_INTERVAL_S"
done
