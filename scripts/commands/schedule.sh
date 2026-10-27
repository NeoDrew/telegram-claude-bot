# commands/schedule.sh - handler for /schedule
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, GLOBAL_SCHED_FILE,
# ROOM_CHAT_ID, CMD, ARGS, CALLBACK_MSG_ID and lib.sh helpers are all in
# scope. Output the reply on stdout; the listener pipes it to tg.sh.
#
# Reads/writes the GLOBAL scheduled.json (state/scheduled.json). All list
# and clear operations filter to ROOM_CHAT_ID so a user in room A only ever
# sees and clears their own tasks (admin override via --all not exposed
# from the menu UI on purpose: "do this for me", not "do this for the bot").

    SUB=$(echo "$ARGS" | awk '{print tolower($1)}')
    REST=$(echo "$ARGS" | cut -d' ' -f2-)
    if [[ "$REST" == "$SUB" ]]; then REST=""; fi
    case "$SUB" in
      ""|list)
        LIST_TEXT=$(CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/schedule_task.sh" list)
        KB=$(GLOBAL_SCHED_FILE="$GLOBAL_SCHED_FILE" FILTER_CHAT="$ROOM_CHAT_ID" python3 <<'PYEOF'
import json, os
try:
    tasks = json.load(open(os.environ['GLOBAL_SCHED_FILE']))
except Exception:
    tasks = []
flt = os.environ.get('FILTER_CHAT', '').strip()
if flt:
    tasks = [t for t in tasks if str(t.get('target_chat_id','')).strip() == flt]
rows = []
for t in tasks[:10]:
    tid = t.get('id', '?')[:8]
    label = (t.get('prompt') or '?').split('\n', 1)[0][:30]
    rows.append([{
        "text": f"x {tid}  {label}",
        "callback_data": f"act:schedule remove {tid}",
    }])
if tasks:
    rows.append([
        {"text": "clear all", "callback_data": "act:schedule clear"},
        {"text": "refresh",   "callback_data": "act:schedule"},
    ])
else:
    rows.append([
        {"text": "refresh", "callback_data": "act:schedule"},
    ])
print(json.dumps(rows))
PYEOF
)
        reply_kb "$LIST_TEXT" "$KB"
        ;;
      clear)
        CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/schedule_task.sh" clear
        ;;
      remove|rm|delete)
        if [[ -z "$REST" ]]; then
          KB=$(GLOBAL_SCHED_FILE="$GLOBAL_SCHED_FILE" FILTER_CHAT="$ROOM_CHAT_ID" python3 <<'PYEOF'
import json, os
try:
    tasks = json.load(open(os.environ['GLOBAL_SCHED_FILE']))
except Exception:
    tasks = []
flt = os.environ.get('FILTER_CHAT', '').strip()
if flt:
    tasks = [t for t in tasks if str(t.get('target_chat_id','')).strip() == flt]
rows = []
for t in tasks[:10]:
    tid = t.get('id', '?')[:8]
    label = (t.get('prompt') or '?').split('\n', 1)[0][:36]
    rows.append([{"text": f"{tid}  {label}",
                  "callback_data": f"act:schedule remove {tid}"}])
if not rows:
    rows = [[{"text": "(no scheduled tasks)", "callback_data": "act:schedule"}]]
print(json.dumps(rows))
PYEOF
)
          CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" kb "Which task to remove?" "$KB" >/dev/null
          exit 0
        fi
        # remove operates on the global file and matches by id prefix.
        # Cross-room safety: refuse to remove a task that doesn't belong to
        # the caller's room (admins could pass --all but the menu UI does
        # not expose that path).
        REMOVE_OUT=$(CHAT_ID="$ROOM_CHAT_ID" GLOBAL_SCHED_FILE="$GLOBAL_SCHED_FILE" \
          TARGET="$REST" FILTER_CHAT="$ROOM_CHAT_ID" python3 <<'PYEOF'
import json, os, sys
p = os.environ['GLOBAL_SCHED_FILE']
target = os.environ['TARGET']
flt = os.environ['FILTER_CHAT'].strip()
try:
    tasks = json.load(open(p))
    if not isinstance(tasks, list):
        tasks = []
except Exception:
    tasks = []
matches = [t for t in tasks
           if t.get('id', '').startswith(target)
           and str(t.get('target_chat_id','')).strip() == flt]
if not matches:
    print(f"No scheduled task in this room matches '{target}'.")
    sys.exit(0)
if len(matches) > 1:
    ids = ', '.join(t.get('id','?')[:8] for t in matches)
    print(f"'{target}' matches {len(matches)} tasks ({ids}). Use a longer prefix.")
    sys.exit(0)
keep = [t for t in tasks if t is not matches[0]]
json.dump(keep, open(p, 'w'), indent=2)
print(f"Removed {matches[0].get('id','?')[:8]}: "
      f"{(matches[0].get('prompt') or '')[:80]}")
PYEOF
)
        OUT=$(GLOBAL_SCHED_FILE="$GLOBAL_SCHED_FILE" FILTER_CHAT="$ROOM_CHAT_ID" \
          REMOVE_OUT="$REMOVE_OUT" python3 <<'PYEOF'
import json, os
try:
    tasks = json.load(open(os.environ['GLOBAL_SCHED_FILE']))
except Exception:
    tasks = []
flt = os.environ.get('FILTER_CHAT', '').strip()
if flt:
    tasks = [t for t in tasks if str(t.get('target_chat_id','')).strip() == flt]
removed_msg = os.environ.get('REMOVE_OUT', '').strip()
rows = []
for t in tasks[:10]:
    tid = t.get('id', '?')[:8]
    label = (t.get('prompt') or '?').split('\n', 1)[0][:36]
    rows.append([{"text": f"{tid}  {label}",
                  "callback_data": f"act:schedule remove {tid}"}])
if rows:
    rows.append([{"text": "done", "callback_data": "act:noop"}])
    head = f"OK {removed_msg}\n\nRemaining tasks, pick another or done."
    print(head + "\x1f" + json.dumps(rows))
else:
    print(f"OK {removed_msg}\n\nNo scheduled tasks remain.\x1f")
PYEOF
)
        HEAD="${OUT%%$'\x1f'*}"
        KB="${OUT#*$'\x1f'}"
        if [[ -z "$KB" ]] || [[ "$KB" == "[]" ]]; then
          echo "$HEAD"
        else
          CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" kb "$HEAD" "$KB" >/dev/null
        fi
        ;;
      *)
        echo "Usage: /schedule [list|clear|remove <id>]
Bare /schedule lists with buttons. /schedule clear wipes all. /schedule remove <prefix> kills one."
        ;;
    esac
