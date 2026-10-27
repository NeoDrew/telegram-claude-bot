#!/bin/bash
# schedule_task.sh - manage globally-scoped scheduled tasks.
#
# Every scheduled task carries an EXPLICIT target_chat_id field. The listener
# fires each task into that chat_id directly. The file lives at
# state/scheduled.json (one file, all rooms).
#
# Project -> room enforcement: state/room_registry.json maps known project
# path/URL prefixes to a required target_chat_id. If a prompt mentions a
# registered prefix and the resolved target_chat_id does not match, this
# script REFUSES with a loud error. This is the guard that prevents the
# "project message in the wrong room" bug.
#
# Usage:
#   # ADD (back-compat positional form; target_chat_id taken from registry
#   # match, then $CHAT_ID env, then .env TELEGRAM_CHAT_ID):
#   schedule_task.sh "prompt" <fire_at_utc_iso>
#   schedule_task.sh "prompt" <fire_at_utc_iso> <interval_seconds>
#
#   # ADD with explicit target (preferred when scheduling cross-room):
#   schedule_task.sh --target-chat-id <id> "prompt" <fire_at_utc_iso> [interval]
#
#   # LIST / REMOVE - default to the caller's CHAT_ID. Pass --all to ignore the
#   # filter and operate on every task in the global file.
#   schedule_task.sh list                # tasks targeting $CHAT_ID
#   schedule_task.sh list --all          # every task across every room
#   schedule_task.sh clear               # remove all tasks targeting $CHAT_ID
#   schedule_task.sh clear --all         # wipe state/scheduled.json
#   schedule_task.sh remove <id|prefix>  # remove one (matches by id prefix)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$REPO_ROOT/state"
SCHED_FILE="$STATE_DIR/scheduled.json"
REGISTRY_PATH="$STATE_DIR/room_registry.json"

mkdir -p "$STATE_DIR"
[[ -f "$SCHED_FILE" ]] || echo "[]" > "$SCHED_FILE"

. "$REPO_ROOT/scripts/lib.sh"

CALLER_CHAT_ID="${CHAT_ID:-$(extract_var TELEGRAM_CHAT_ID)}"

# Pull the caller room's active_cwd from sessions.json so registry auto-learn
# can bind /Users/<u>/<workspace>/<project>/ to this target_chat_id even when
# the prompt itself never mentions the path. Silently absent on the first
# message into a fresh room; that is fine, we fall back to prompt-only.
caller_room_cwd() {
  local sf="$STATE_DIR/rooms/$CALLER_CHAT_ID/sessions.json"
  [[ -f "$sf" ]] || { echo ""; return; }
  python3 -c "
import json, sys
try:
    d = json.load(open('$sf'))
    print(d.get('active_cwd', '') if isinstance(d, dict) else '')
except Exception:
    pass
"
}

# Parse leading flags. We only accept --target-chat-id <id> here for ADD.
# list/clear/remove subcommands parse their own --all flag below.
TARGET_CHAT_ARG=""
CWD_ARG=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --target-chat-id)
      TARGET_CHAT_ARG="${2:-}"
      shift 2
      ;;
    --cwd)
      CWD_ARG="${2:-}"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

# Subcommands. These intentionally come AFTER flag parsing so callers can
# write `schedule_task.sh list --all` OR `schedule_task.sh --target-chat-id X
# list` interchangeably. (Add path uses positional args.)
SUB="${1:-}"

filter_tasks_py() {
  # Args: $1=mode (list|count|ids), $2=chat_id ("" for all)
  MODE="$1" FILTER_CHAT="$2" SCHED_FILE="$SCHED_FILE" python3 <<'PYEOF'
import json, os, sys, time
mode = os.environ['MODE']
flt = os.environ.get('FILTER_CHAT', '')
try:
    with open(os.environ['SCHED_FILE']) as f:
        tasks = json.load(f)
    if not isinstance(tasks, list):
        tasks = []
except Exception:
    tasks = []
if flt:
    tasks = [t for t in tasks if str(t.get('target_chat_id','')).strip() == flt.strip()]
if mode == 'count':
    print(len(tasks))
    sys.exit(0)
if mode == 'ids':
    for t in tasks:
        print(t.get('id', ''))
    sys.exit(0)
# mode == 'list'
if not tasks:
    print("(no scheduled tasks)")
    sys.exit(0)
now = int(time.time())
print(f"{len(tasks)} scheduled task(s):")
for t in tasks:
    fire = int(t.get('fire_at_epoch', 0))
    delta = fire - now
    if delta < 0:
        when = f"{-delta}s overdue"
    elif delta < 60:
        when = f"in {delta}s"
    elif delta < 3600:
        when = f"in {delta // 60}m {delta % 60}s"
    else:
        when = f"in {delta // 3600}h {(delta % 3600) // 60}m"
    kind = f"every {t.get('interval_seconds')}s" if t.get('recurring') else "one-shot"
    tid = t.get('id', '?')[:8]
    chat = t.get('target_chat_id', '?')
    prompt = (t.get('prompt') or '').replace('\n', ' ')[:80]
    print(f"  {tid}  -> chat={chat:<14}  {kind:<14}  {when:<18}  {prompt}")
PYEOF
}

case "$SUB" in
  list)
    SCOPE="${2:-}"
    if [[ "$SCOPE" == "--all" ]]; then
      filter_tasks_py list ""
    else
      filter_tasks_py list "$CALLER_CHAT_ID"
    fi
    exit 0
    ;;
  clear)
    SCOPE="${2:-}"
    if [[ "$SCOPE" == "--all" ]]; then
      echo "[]" > "$SCHED_FILE"
      echo "Cleared all scheduled tasks (global)."
      exit 0
    fi
    FILTER="$CALLER_CHAT_ID" SCHED_FILE="$SCHED_FILE" python3 <<'PYEOF'
import json, os
p = os.environ['SCHED_FILE']
flt = os.environ['FILTER']
try:
    tasks = json.load(open(p))
    if not isinstance(tasks, list):
        tasks = []
except Exception:
    tasks = []
keep = [t for t in tasks if str(t.get('target_chat_id','')).strip() != flt.strip()]
removed = len(tasks) - len(keep)
tmp = f"{p}.tmp.{os.getpid()}"
json.dump(keep, open(tmp, 'w'), indent=2)
os.replace(tmp, p)
print(f"Cleared {removed} task(s) targeting chat={flt}.")
PYEOF
    exit 0
    ;;
  remove)
    if [[ -z "${2:-}" ]]; then
      echo "Usage: schedule_task.sh remove <task_id_or_prefix>" >&2
      exit 1
    fi
    TARGET="$2" SCHED_FILE="$SCHED_FILE" python3 <<'PYEOF'
import json, os, sys
p = os.environ['SCHED_FILE']
target = os.environ['TARGET']
try:
    tasks = json.load(open(p))
    if not isinstance(tasks, list):
        tasks = []
except Exception:
    tasks = []
matches = [t for t in tasks if t.get('id', '').startswith(target)]
if not matches:
    print(f"No scheduled task matches '{target}'.")
    sys.exit(0)
if len(matches) > 1:
    ids = ', '.join(t.get('id', '?')[:8] for t in matches)
    print(f"'{target}' matches {len(matches)} tasks ({ids}). Use a longer prefix.")
    sys.exit(0)
keep = [t for t in tasks if t is not matches[0]]
tmp = f"{p}.tmp.{os.getpid()}"
json.dump(keep, open(tmp, 'w'), indent=2)
os.replace(tmp, p)
print(f"Removed {matches[0].get('id', '?')[:8]} (was targeting chat="
      f"{matches[0].get('target_chat_id','?')}): "
      f"{(matches[0].get('prompt') or '')[:80]}")
PYEOF
    exit 0
    ;;
esac

# Positional ADD form. Requires <prompt> <fire_at_iso> [interval].
if [[ $# -lt 2 ]]; then
  echo "Usage:
  schedule_task.sh [--target-chat-id <id>] [--cwd <path>] <prompt> <fire_at_iso_utc> [interval_seconds]
  schedule_task.sh list [--all]
  schedule_task.sh clear [--all]
  schedule_task.sh remove <task_id_prefix>" >&2
  exit 1
fi

PROMPT="$1"
FIRE_AT_ISO="$2"
INTERVAL="${3:-}"

# Resolve target_chat_id: explicit flag wins; else registry resolution from
# prompt; else caller's CHAT_ID. The registry validation step below will
# REFUSE if the resolved target conflicts with a registered project.
TARGET_CHAT_ID="$TARGET_CHAT_ARG"
# Default cwd to the caller room's active_cwd if --cwd was not given. The
# registry uses it both for auto-learn (binding the cwd's project to the
# target room) and as a tiebreak when the prompt itself is silent about
# its project.
if [[ -z "$CWD_ARG" ]]; then
  CWD_ARG="$(caller_room_cwd)"
fi
if [[ -z "$TARGET_CHAT_ID" ]]; then
  RESOLVED=$(python3 "$REPO_ROOT/scripts/registry.py" resolve "$PROMPT" "$CWD_ARG" 2>/dev/null)
  if [[ -n "$RESOLVED" ]]; then
    TARGET_CHAT_ID="$RESOLVED"
  else
    TARGET_CHAT_ID="$CALLER_CHAT_ID"
  fi
fi

if [[ -z "$TARGET_CHAT_ID" ]]; then
  echo "schedule_task.sh: cannot resolve target_chat_id (no --target-chat-id flag, no registry match, no CHAT_ID env, no .env TELEGRAM_CHAT_ID)" >&2
  exit 1
fi

# Registry validation. Refuses if the prompt belongs to a known project that
# requires a different target. The error message tells the caller exactly
# which chat is required and where to fix the mapping.
if ! python3 "$REPO_ROOT/scripts/registry.py" check "$TARGET_CHAT_ID" "$PROMPT" "$CWD_ARG"; then
  exit 2
fi

# Convert ISO8601 UTC -> epoch.
FIRE_EPOCH=$(python3 -c "
import sys
from datetime import datetime, timezone
iso = '$FIRE_AT_ISO'.replace('Z', '+00:00')
try:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    print(int(dt.timestamp()))
except Exception as e:
    print('ERR: ' + str(e), file=sys.stderr)
    sys.exit(1)
")
if [[ $? -ne 0 ]]; then
  echo "Bad fire_at timestamp: $FIRE_AT_ISO" >&2
  exit 1
fi

TASK_ID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")

SCHED_FILE="$SCHED_FILE" \
TARGET_CHAT_ID="$TARGET_CHAT_ID" \
TASK_ID="$TASK_ID" \
PROMPT_ENV="$PROMPT" \
FIRE_EPOCH="$FIRE_EPOCH" \
FIRE_ISO="$FIRE_AT_ISO" \
INTERVAL="$INTERVAL" \
CWD_ARG="$CWD_ARG" \
python3 <<'PYEOF'
import json, os, sys
sched_file = os.environ['SCHED_FILE']
target     = os.environ['TARGET_CHAT_ID']
task_id    = os.environ['TASK_ID']
prompt     = os.environ['PROMPT_ENV']
fire_epoch = int(os.environ['FIRE_EPOCH'])
fire_iso   = os.environ['FIRE_ISO']
interval   = os.environ['INTERVAL']
cwd        = os.environ.get('CWD_ARG', '')
try:
    with open(sched_file) as f:
        tasks = json.load(f)
    if not isinstance(tasks, list):
        tasks = []
except (FileNotFoundError, json.JSONDecodeError):
    tasks = []
entry = {
    "id": task_id,
    "target_chat_id": str(target),
    "prompt": prompt,
    "fire_at_epoch": fire_epoch,
    "fire_at_human": fire_iso,
    "recurring": bool(interval and interval != ""),
}
if cwd:
    entry["cwd"] = cwd
if interval and interval != "":
    entry["interval_seconds"] = int(interval)
tasks.append(entry)
tmp = f"{sched_file}.tmp.{os.getpid()}"
with open(tmp, "w") as f:
    json.dump(tasks, f, indent=2)
os.replace(tmp, sched_file)
print(task_id)
PYEOF

# Auto-learn: persist any project prefixes detected in the prompt + cwd as
# bound to this target_chat_id. Subsequent schedules with the same prefix
# from a different room will be refused by registry.validate_target. This
# is what makes the bot robust to "new project, never seen before" without
# any manual config.
python3 "$REPO_ROOT/scripts/registry.py" learn "$TARGET_CHAT_ID" "$PROMPT" "$CWD_ARG" 2>/dev/null || true
