# commands/cdbrowse.sh - handler for act:cdbrowse (drill-down folder picker)
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, CMD, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers (reply_kb, ...) are all in scope.
#
# ARGS is a path relative to the projects dir, BOT_PROJECTS_DIR (e.g.
# "myrepo" or "myrepo/service"),
# never absolute. This exists purely so the bare /cd keyboard can reach
# subfolders that would otherwise never appear as buttons (it only lists
# the projects dir's immediate children). Selecting a folder always goes back through
# the existing "cd" verb (act:cd <relpath>), so the actual cwd-switch /
# session-drop logic lives in exactly one place: cd.sh's resolve().

KB_AND_HEAD=$(REL="$ARGS" HOME_DIR="$HOME" REPO_ROOT="$REPO_ROOT" SESSIONS_FILE="$SESSIONS_FILE" python3 <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
import room_state  # single source of truth for sessions.json
home = os.environ['HOME_DIR']
projects = os.environ.get('BOT_PROJECTS_DIR') or home
rel = os.environ['REL'].strip().strip('/')
cur = room_state.get_cwd(os.environ['SESSIONS_FILE'])

full = os.path.realpath(os.path.join(projects, rel)) if rel else projects
if not (full == home or full.startswith(home + '/')):
    print(f"Refused: `{full}` is outside `{home}`" + "\x1f" + json.dumps([]))
    sys.exit(0)
if not os.path.isdir(full):
    print(f"Not found: `{rel}`" + "\x1f" + json.dumps([]))
    sys.exit(0)

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

children = []
try:
    for name in sorted(os.listdir(full), key=str.lower):
        p = os.path.join(full, name)
        if os.path.isdir(p) and not name.startswith('.'):
            children.append((name, p))
except OSError:
    pass

CAP = 30
rows = [[{"text": "✓ Use this folder", "callback_data": f"act:cd {rel}"}]]
parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
up_cb = f"act:cdbrowse {parent}" if parent else "act:cd"
rows.append([{"text": "⬆ Up", "callback_data": up_cb}])
for name, p in children[:CAP]:
    child_rel = f"{rel}/{name}" if rel else name
    cmark = '✓ ' if p == cur else ''
    row = [{"text": f"{cmark}{name}", "callback_data": f"act:cd {child_rel}"}]
    if has_subdirs(p):
        row.append({"text": "▸", "callback_data": f"act:cdbrowse {child_rel}"})
    rows.append(row)

mark = '✓ ' if full == cur else ''
head = f"{mark}📁 `{rel}`"
if len(children) > CAP:
    head += f"\n_+{len(children) - CAP} more_"
if not children:
    head += "\n_No subfolders._"
print(head + "\x1f" + json.dumps(rows))
PYEOF
)
HEAD="${KB_AND_HEAD%%$'\x1f'*}"
KB="${KB_AND_HEAD#*$'\x1f'}"
reply_kb "$HEAD" "$KB"
