#!/bin/bash
# doctor.sh - one-shot health snapshot for a single room.
#
# Answers the questions "is claude alive?", "what's it doing right now?",
# "is the network OK?", "how long since the stream last moved?", "where's
# the audit log if I want to dig deeper?", all on one screen.
#
# Designed to be safe to run against a running spawn: read-only, no signals.
#
# Usage:
#   scripts/doctor.sh                   # auto-pick most active room
#   scripts/doctor.sh <chat_id>
#   scripts/doctor.sh --all             # one block per active room

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOMS_DIR="$REPO_ROOT/state/rooms"
LOGS_ROOMS="$REPO_ROOT/logs/rooms"

usage() {
  cat <<EOF
Usage: $0 [<chat_id>|--all]
Prints a health snapshot per room.
EOF
}

# Pretty: emit a section header with a divider so multiple rooms scan easily.
section() {
  local title="$1"
  echo
  echo "═══ $title ═══"
}

# Format an absolute byte count as human-readable.
human_bytes() {
  local b="${1:-0}"
  awk -v b="$b" 'BEGIN{
    if (b<1024) {printf "%dB", b; exit}
    if (b<1048576) {printf "%.1fKB", b/1024; exit}
    if (b<1073741824) {printf "%.1fMB", b/1048576; exit}
    printf "%.1fGB", b/1073741824
  }'
}

# Format epoch as relative "Ns ago".
ago() {
  local epoch="${1:-0}"
  if [[ "$epoch" == 0 ]]; then echo "never"; return; fi
  local now diff
  now=$(date +%s)
  diff=$((now - epoch))
  if   (( diff < 60 ));    then echo "${diff}s ago"
  elif (( diff < 3600 ));  then echo "$((diff/60))m $((diff%60))s ago"
  elif (( diff < 86400 )); then echo "$((diff/3600))h $((diff%3600/60))m ago"
  else echo "$((diff/86400))d ago"
  fi
}

# Test a single chat. All read-only.
diagnose_room() {
  local chat="$1"
  local rd="$ROOMS_DIR/$chat"
  local ld="$LOGS_ROOMS/$chat"

  if [[ ! -d "$rd" ]]; then
    echo "room $chat: no state directory at $rd"
    return 1
  fi

  section "ROOM $chat"

  # Label (if set via /label)
  local label
  label=$(cat "$rd/label" 2>/dev/null || echo "")
  [[ -n "$label" ]] && echo "label:           $label"

  # Active cwd + session
  local cwd sid
  cwd=$(python3 "$REPO_ROOT/scripts/room_state.py" get-cwd "$rd/sessions.json" 2>/dev/null)
  sid=$(python3 "$REPO_ROOT/scripts/room_state.py" get-session "$rd/sessions.json" 2>/dev/null)
  echo "cwd:             $cwd"
  echo "session:         ${sid:-(fresh)}"

  # Spawn classification via spawn_state (the same source /menu and /status use)
  local state_json state tool_name tool_preview duration sid_state wrapper_pid_alive wrapper_pid
  state_json=$(python3 "$REPO_ROOT/scripts/spawn_state.py" "$rd" 2>/dev/null || echo '{}')
  state=$(echo "$state_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state","?"))')
  tool_name=$(echo "$state_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_name") or "")')
  tool_preview=$(echo "$state_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_preview") or "")')
  duration=$(echo "$state_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("duration_s") or "")')
  wrapper_pid_alive=$(echo "$state_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("wrapper_pid_alive"))')
  wrapper_pid=$(echo "$state_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("wrapper_pid") or "")')

  local state_line="state:           $state"
  if [[ -n "$tool_name" ]]; then
    state_line+="  →  $tool_name"
    [[ -n "$tool_preview" ]] && state_line+=" \`$tool_preview\`"
  fi
  if [[ -n "$duration" ]]; then
    state_line+="  ($duration s)"
  fi
  echo "$state_line"
  echo "wrapper pid:     ${wrapper_pid:-none}  alive=$wrapper_pid_alive"

  # If wrapper alive, find the claude child + its uptime/cpu.
  if [[ "$wrapper_pid_alive" == "True" ]] && [[ -n "$wrapper_pid" ]]; then
    local claude_pid claude_row
    for child in $(pgrep -P "$wrapper_pid" 2>/dev/null); do
      local cmd
      cmd=$(ps -p "$child" -o command= 2>/dev/null)
      if [[ "$cmd" == *claude* ]] && \
         { [[ "$cmd" == *--session-id* ]] || [[ "$cmd" == *--resume* ]]; }; then
        claude_pid=$child
        break
      fi
    done
    if [[ -n "$claude_pid" ]]; then
      claude_row=$(ps -p "$claude_pid" -o etime=,pcpu=,rss=,stat= 2>/dev/null | head -1)
      echo "claude pid:      $claude_pid  (etime/cpu/rss/stat: $claude_row)"

      # TCP state to api.anthropic.com (read-only). BSD lsof ORs -p and
      # -iTCP without the -a flag (returning every TCP socket on the box).
      # We need the intersection: only THIS process's TCP files. Hence -a.
      local tcp_summary
      tcp_summary=$(/usr/sbin/lsof -nP -a -p "$claude_pid" -iTCP 2>/dev/null | \
        awk 'NR>1 && /443/ {
          state="?"
          for (i=NF; i>=1; i--) if ($i ~ /^\(.*\)$/) {state=$i; gsub(/[()]/, "", state); break}
          print state
        }' | sort | uniq -c | awk '{printf "%d×%s  ", $1, $2}')
      echo "TCP→api:443:     ${tcp_summary:-(none)}"
    fi
  fi

  # Audit log freshness
  if [[ -f "$ld/claude.stream.jsonl" ]]; then
    local sl_mtime sl_size
    sl_mtime=$(stat -f "%m" "$ld/claude.stream.jsonl" 2>/dev/null || echo 0)
    sl_size=$(stat -f "%z" "$ld/claude.stream.jsonl" 2>/dev/null || echo 0)
    echo "audit log:       $(human_bytes $sl_size)  last write $(ago $sl_mtime)"
  else
    echo "audit log:       (none yet; spawn hasn't run since the audit log was added)"
  fi

  # Diagnostics sidecar (added recently; older spawns may not have it)
  if [[ -f "$ld/spawn_diag.jsonl" ]]; then
    local dl_lines dl_mtime
    dl_lines=$(wc -l < "$ld/spawn_diag.jsonl" 2>/dev/null | tr -d ' ')
    dl_mtime=$(stat -f "%m" "$ld/spawn_diag.jsonl" 2>/dev/null || echo 0)
    local stall_count
    # grep -c exits 1 when the count is 0; capture stderr-free and treat any
    # empty/missing result as 0 (the `|| echo 0` form double-emits via the
    # OR branch AND the captured stdout, leaving a stray "0" line).
    stall_count=$(grep -c stall_detected "$ld/spawn_diag.jsonl" 2>/dev/null || true)
    stall_count=${stall_count:-0}
    echo "diag log:        $dl_lines snapshots  last write $(ago $dl_mtime)  stalls=$stall_count"
    # Surface any forensic dumps that haven't been pruned.
    local stalls
    stalls=$(ls -t "$ld"/stall_*.txt 2>/dev/null | head -3)
    if [[ -n "$stalls" ]]; then
      echo "forensic dumps:"
      echo "$stalls" | sed 's/^/  /'
    fi
  fi

  # Last few rows of the interpreted stream: gives a sense of "what was
  # claude just doing?" without running show_stream separately. We pipe via
  # show_stream so the format stays consistent.
  if [[ -f "$ld/claude.stream.jsonl" ]]; then
    echo
    echo "── last 5 events ──"
    python3 "$REPO_ROOT/scripts/show_stream.py" --room "$ld" --tail 5 2>/dev/null | sed 's/^/  /' | head -10
  fi

  # Quick external reachability: TCP-connect to api.anthropic.com:443.
  # nc -G is the connect timeout. Sub-second on a healthy network.
  local t0 t1 ok
  t0=$(python3 -c 'import time; print(int(time.time()*1000))')
  if /usr/bin/nc -z -G 3 api.anthropic.com 443 2>/dev/null; then ok=ok; else ok="FAIL"; fi
  t1=$(python3 -c 'import time; print(int(time.time()*1000))')
  echo
  echo "api.anthropic.com:443 reachable: $ok  (${t0}→${t1}, $((t1-t0))ms)"
}

# --- main --------------------------------------------------------------------

case "${1:-}" in
  -h|--help)
    usage; exit 0 ;;
  --all)
    if [[ ! -d "$ROOMS_DIR" ]]; then
      echo "no rooms dir"; exit 1
    fi
    for d in "$ROOMS_DIR"/*; do
      [[ -d "$d" ]] || continue
      diagnose_room "$(basename "$d")"
    done
    ;;
  "")
    # Auto-pick: most-recently-active room by busy.lock mtime, then
    # falling back to inbox.ndjson, then sessions.json.
    if [[ ! -d "$ROOMS_DIR" ]]; then
      echo "no rooms dir"; exit 1
    fi
    most_recent=""
    most_recent_mtime=0
    for d in "$ROOMS_DIR"/*; do
      [[ -d "$d" ]] || continue
      for f in busy.lock inbox.ndjson sessions.json; do
        if [[ -f "$d/$f" ]]; then
          m=$(stat -f "%m" "$d/$f" 2>/dev/null || echo 0)
          if (( m > most_recent_mtime )); then
            most_recent_mtime=$m
            most_recent="$d"
          fi
          break
        fi
      done
    done
    if [[ -z "$most_recent" ]]; then
      echo "no rooms found"; exit 1
    fi
    diagnose_room "$(basename "$most_recent")"
    ;;
  *)
    diagnose_room "$1"
    ;;
esac
