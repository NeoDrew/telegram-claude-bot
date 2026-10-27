# commands/cd.sh - handler for /cd
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    if [[ -z "$ARGS" ]]; then
      # Bare /cd: flat keyboard of every folder in the projects dir
      # (BOT_PROJECTS_DIR, default: home). The previous
      # in-progress-vs-fresh split is gone, since /cd to a new dir always
      # drops the previous session now. No reason to advertise "resume"
      # behaviour that no longer exists.
      KB_AND_HEAD=$(SESSIONS_FILE="$SESSIONS_FILE" HOME_DIR="$HOME" REPO_ROOT="$REPO_ROOT" python3 <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
import room_state  # single source of truth for sessions.json
home = os.environ['HOME_DIR']
projects = os.environ.get('BOT_PROJECTS_DIR') or home
cur = room_state.get_cwd(os.environ['SESSIONS_FILE'])

def has_subdirs(path):
    try:
        for name in os.listdir(path):
            if name.startswith('.'):
                continue
            if os.path.isdir(os.path.join(path, name)):
                return True
    except OSError:
        pass
    return False

entries = []
if os.path.isdir(projects):
    for name in sorted(os.listdir(projects), key=str.lower):
        full = os.path.join(projects, name)
        if os.path.isdir(full) and not name.startswith('.'):
            entries.append((name, full))

# Each directory gets its own row: the name (direct select, unchanged
# behaviour) plus a "▸" browse button when it has subfolders worth drilling
# into (e.g. a monorepo with one folder per app or service).
# Leaf directories (the common case for ordinary code repos) get no "▸" and
# look exactly as before.
CAP = 30
rows = []
for name, full in entries[:CAP]:
    mark = '✓ ' if full == cur else ''
    row = [{"text": f"{mark}{name}", "callback_data": f"act:cd {name}"}]
    if has_subdirs(full):
        row.append({"text": "▸", "callback_data": f"act:cdbrowse {name}"})
    rows.append(row)

head = f"Active cwd: `{cur or '(none)'}`\n_Switching directories starts a fresh chat. The previous session is lost._\n_Tap ▸ to browse into a subfolder._"
if len(entries) > CAP:
    head += f"\n_+{len(entries) - CAP} more not shown; use the ▸ buttons to browse into a folder._"
if not rows:
    rows = [[{"text": f"(no folders in {projects})", "callback_data": "decor:empty"}]]
print(head + "\x1f" + json.dumps(rows))
PYEOF
)
      HEAD="${KB_AND_HEAD%%$'\x1f'*}"
      KB="${KB_AND_HEAD#*$'\x1f'}"
      reply_kb "$HEAD" "$KB"
      exit 0
    fi
    # Switching folders mid-reply stops that reply, so ask first. BUG
    # FIXED: the tap used to cut the reply off with no warning. "--now"
    # (the confirm button) skips the question.
    if [[ "$ARGS" == "--now "* ]]; then
      ARGS="${ARGS#--now }"
    elif is_busy "$ROOM_CHAT_ID"; then
      CD_STATE=$(python3 "$REPO_ROOT/scripts/spawn_state.py" "$ROOM_DIR" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
      CD_CB="act:cd --now $ARGS"
      # Telegram caps callback_data at 64 bytes; a longer path just switches.
      if [[ "$CD_STATE" =~ ^(streaming|tool|awaiting_result|compacting)$ && ${#CD_CB} -le 64 ]]; then
        CD_KB=$(CD_CB="$CD_CB" python3 -c 'import json,os
print(json.dumps([[{"text": "Switch and stop it", "callback_data": os.environ["CD_CB"]},
                   {"text": "Keep going", "callback_data": "act:noop"}]]))')
        reply_kb "A reply is still running here. Switching to \`$ARGS\` stops it and starts a fresh session." "$CD_KB" 0
        exit 0
      fi
    fi
    # Resolve the target directory, update sessions.json, and print the
    # user-visible reply. If the cwd actually changed, the block appends
    # a "__CD_DROPPED_SESSION__" sentinel line so the surrounding shell
    # tears down any in-flight spawn (it was running in the old cwd).
    CHANGE_OUT=$(SESSIONS_FILE="$SESSIONS_FILE" ARGS="$ARGS" HOME_DIR="$HOME" REPO_ROOT="$REPO_ROOT" python3 <<'PYEOF'
import os, sys, fnmatch
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
import room_state  # single source of truth for sessions.json
p = os.environ['SESSIONS_FILE']
arg = os.environ['ARGS'].strip()
home = os.environ['HOME_DIR']
projects = os.environ.get('BOT_PROJECTS_DIR') or home

bases = [projects] + ([home] if home != projects else [])

def resolve():
    if os.path.isabs(arg):
        return ('ok', arg) if os.path.isdir(arg) else ('err', f"Path not found: {arg}")
    if arg.startswith('~'):
        exp = os.path.expanduser(arg)
        return ('ok', exp) if os.path.isdir(exp) else ('err', f"Path not found: {arg}")
    for base in bases:
        cand = os.path.join(base, arg)
        if os.path.isdir(cand):
            return ('ok', cand)
    pattern = arg if any(c in arg for c in '*?[') else arg + '*'
    matches = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        try:
            for entry in sorted(os.listdir(base)):
                if fnmatch.fnmatchcase(entry.lower(), pattern.lower()):
                    full = os.path.join(base, entry)
                    if os.path.isdir(full):
                        matches.append(full)
        except OSError:
            continue
        if matches:
            break
    seen = set()
    matches = [m for m in matches if not (m in seen or seen.add(m))]
    if len(matches) == 1:
        return ('ok', matches[0])
    if len(matches) > 1:
        listing = "\n".join(f"  - {m}" for m in matches[:20])
        return ('err', f"Pattern '{pattern}' matches {len(matches)} directories:\n{listing}\nBe more specific.")
    return ('err', f"Pattern '{pattern}' matches nothing under " + " or ".join(bases) + ".")

kind, val = resolve()
if kind == 'err':
    print(val)
    sys.exit(0)

candidate = os.path.realpath(val)
if not (candidate == home or candidate.startswith(home + '/')):
    print(f"Refused: `{candidate}` is outside `{home}`")
    sys.exit(0)

prev_cwd = room_state.get_cwd(p)
prev_sid = room_state.get_session(p)

if candidate == prev_cwd:
    out = f"Already in `{candidate}`."
    if prev_sid:
        out += f" Session `{prev_sid[:8]}…` still active."
    room_state.write(p, cwd=candidate, session=prev_sid)
    print(out)
    sys.exit(0)

room_state.write(p, cwd=candidate, session='')   # different dir -> drop session
out = f"Active cwd: `{candidate}`"
if prev_sid:
    out += (f"\nPrevious session in `{prev_cwd}` discarded "
            f"(`{prev_sid[:8]}…`). Next message starts fresh.")
else:
    out += "\nNext message starts a fresh session."
print(out)
print("__CD_DROPPED_SESSION__")
PYEOF
)
    # If the python block decided to drop the session, kill any in-flight
    # spawn. The session is gone; the spawn would otherwise keep running
    # in the OLD cwd and inject the user's next message into a dead-end
    # context.
    if [[ "$CHANGE_OUT" == *"__CD_DROPPED_SESSION__"* ]]; then
      CHANGE_OUT="${CHANGE_OUT//$'\n'__CD_DROPPED_SESSION__/}"
      CHANGE_OUT="${CHANGE_OUT//__CD_DROPPED_SESSION__/}"
      kill_room_spawn "$ROOM_DIR" 1
    fi
    echo "$CHANGE_OUT"

    # Auto-rename group chats to "[original name] - (cwd)" so the title
    # stays useful. Groups have negative chat_ids; DMs are skip.
    # Silently no-ops if the bot isn't an admin or the cwd didn't change.
    if [[ "${ROOM_CHAT_ID:-0}" == -* ]] && [[ "$CHANGE_OUT" != *"Already in"* ]]; then
      (
        export CHAT_ID="$ROOM_CHAT_ID"
        # Load or derive the base title (strip any existing " - (...)" suffix).
        base_title_file="$ROOM_DIR/.base_chat_title"
        if [[ -f "$base_title_file" ]]; then
          base_title=$(cat "$base_title_file")
        else
          current_title=$("$REPO_ROOT/scripts/tg.sh" get_chat_title 2>/dev/null || echo "")
          # Strip trailing " - (...)" suffix to recover the original name.
          base_title=$(printf '%s' "$current_title" | sed 's/ - ([^)]*)$//')
          [[ -n "$base_title" ]] && printf '%s' "$base_title" > "$base_title_file"
        fi
        if [[ -n "$base_title" ]]; then
          new_cwd=$(python3 -c "
import json, sys
p = '$SESSIONS_FILE'
import os; sys.path.insert(0, '$REPO_ROOT/scripts')
import room_state
print(room_state.get_cwd(p) or '')
" 2>/dev/null || echo "")
          if [[ -n "$new_cwd" ]]; then
            cwd_name=$(basename "$new_cwd")
            new_title="${base_title} - (${cwd_name})"
            "$REPO_ROOT/scripts/tg.sh" set_chat_title "$new_title" 2>/dev/null || true
          fi
        fi
      ) &
    fi
