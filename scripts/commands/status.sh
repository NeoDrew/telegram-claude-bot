# commands/status.sh - handler for /status
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Full multi-line status report. (/ping was removed and folded into
    # /status; the mode=='/ping' one-liner branch below is now dead but left
    # in place so re-adding a /ping route would just work.)
    SESSIONS_FILE="$SESSIONS_FILE" \
    BUSY_LOCK="$BUSY_LOCK" \
    QUEUE_FILE="$QUEUE_FILE" \
    EFFORT_FILE="$EFFORT_FILE" \
    MODEL_FILE="$MODEL_FILE" \
    SPAWN_LOG="$REPO_ROOT/logs/spawn.log" \
    BOT_LABEL="$(bot_label)" \
    MODE="$CMD" \
    ROOM_CHAT_ID="$ROOM_CHAT_ID" \
    ROOM_LABEL="$(cat "$ROOM_DIR/label" 2>/dev/null || echo "")" \
    ROOM_DIR="$ROOM_DIR" \
    REPO_ROOT="$REPO_ROOT" \
    python3 <<'PYEOF'
import json, os, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

mode = os.environ['MODE']  # "/status" or "/ping"


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


def proc_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def humanize(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def format_event(ev):
    """Map a transcript NDJSON event to (label, datetime|None) or None to skip."""
    t = ev.get('type')
    ts_raw = ev.get('timestamp') or ((ev.get('message') or {}).get('timestamp'))
    ts = None
    if ts_raw:
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace('Z', '+00:00'))
        except Exception:
            ts = None
    if t == 'assistant':
        msg = ev.get('message') or {}
        for block in (msg.get('content') or []):
            btype = block.get('type')
            if btype == 'text':
                text = (block.get('text') or '').strip().split('\n', 1)[0]
                if text:
                    return (f"📝 {text[:90]}", ts)
            if btype == 'tool_use':
                name = block.get('name', 'tool')
                args = block.get('input') or {}
                preview = ''
                if name == 'Bash':
                    preview = (args.get('command') or '').replace('\n', ' ')[:80]
                elif name in ('Read', 'Write', 'Edit'):
                    preview = (args.get('file_path') or '').split('/')[-1]
                elif isinstance(args, dict):
                    preview = ', '.join(
                        f"{k}={str(v)[:30]}" for k, v in list(args.items())[:2])
                arrow = f"({preview})" if preview else ''
                return (f"🔧 {name}{arrow}"[:120], ts)
            if btype == 'thinking':
                return ("💭 thinking", ts)
        return None
    if t == 'user':
        msg = ev.get('message') or {}
        content = msg.get('content')
        if isinstance(content, str):
            text = content.strip().split('\n', 1)[0]
            if text:
                return (f"👤 {text[:90]}", ts)
        elif isinstance(content, list):
            if any(c.get('type') == 'tool_result'
                   for c in content if isinstance(c, dict)):
                return None
            for c in content:
                if isinstance(c, dict) and c.get('type') == 'text':
                    text = (c.get('text') or '').strip().split('\n', 1)[0]
                    if text:
                        return (f"👤 {text[:90]}", ts)
        return None
    return None

# ---- spawn state (real turn-level classification, not just "wrapper alive") ----
busy_pid = read_text(os.environ['BUSY_LOCK'])
wrapper_alive = busy_pid and proc_alive(busy_pid)
try:
    import subprocess as _sp
    _state_json = _sp.check_output(
        ['python3', os.environ['REPO_ROOT'] + '/scripts/spawn_state.py',
         os.environ['ROOM_DIR']],
        text=True, timeout=5)
    spawn_state = json.loads(_state_json)
except Exception:
    spawn_state = {"state": "no_spawn"}

def humanize_secs(s):
    if s is None: return ""
    s = int(s)
    if s < 60: return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60: return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"

def render_state(st):
    s = st.get('state', '?')
    if s == 'idle':
        return "💤 idle"
    if s == 'streaming':
        return "💬 streaming"
    if s == 'tool':
        tn = st.get('tool_name', '?')
        prev = st.get('tool_preview', '') or ''
        dur = humanize_secs(st.get('duration_s'))
        if prev:
            return f"🔧 in tool: {tn}({prev}) · {dur}"
        return f"🔧 in tool: {tn} · {dur}"
    if s == 'awaiting_result':
        return "⏳ awaiting result"
    if s == 'no_session':
        return "⚪ no session"
    return "⚪ no spawn"

# Legacy "busy" = wrapper alive AND state isn't idle/no_*. This keeps
# the rest of the rendering simple while the detailed state appears
# on its own line.
busy = wrapper_alive and spawn_state.get('state') not in ('idle', 'no_session', 'no_spawn')

# ---- queue depth ----
qf = os.environ['QUEUE_FILE']
queue = 0
if os.path.exists(qf):
    with open(qf) as f:
        queue = sum(1 for line in f if line.strip())

# ---- effort ----
effort = read_text(os.environ['EFFORT_FILE']) or "default"
# ---- model ----
import sys as _sys
_sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import default_label
model = read_text(os.environ.get('MODEL_FILE', '')) or f"default ({default_label()})"
# Drop the redundant "claude-" prefix for display (e.g. "claude-opus-4-8[1m]"
# -> "opus-4-8[1m]"); the "default (...)" fallback is left as-is.
if model.startswith("claude-"):
    model = model[7:]

# ---- bot uptime via launchctl + ps etime ----
uptime = "?"
try:
    out = subprocess.check_output(
        ['launchctl', 'list'], text=True, timeout=5)
    bot_pid = None
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) >= 3 and parts[2].strip() == os.environ['BOT_LABEL']:
            try:
                bot_pid = int(parts[0])
            except ValueError:
                pass
            break
    if bot_pid:
        etime = subprocess.check_output(
            ['ps', '-p', str(bot_pid), '-o', 'etime='],
            text=True, timeout=5).strip()
        # ps etime: "MM:SS", "HH:MM:SS", or "DD-HH:MM:SS"
        days = 0
        rest = etime
        if '-' in rest:
            d, rest = rest.split('-', 1)
            days = int(d)
        parts = rest.split(':')
        if len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        else:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        if days:
            uptime = f"{days}d {h}h"
        elif h:
            uptime = f"{h}h {m}m"
        elif m:
            uptime = f"{m}m {s}s"
        else:
            uptime = f"{s}s"
except Exception:
    pass

# ---- last spawn exit (parse tail of spawn.log) ----
last_spawn = ""
try:
    lines = Path(os.environ['SPAWN_LOG']).read_text().splitlines()
    for line in reversed(lines):
        if " exit=" in line:
            last_spawn = line.strip()
            break
except Exception:
    pass

# ---- session info ----
# room_state.py owns the sessions.json schema (flat shape + legacy fallback).
import sys as _sys
_sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
import room_state
cwd = room_state.get_cwd(os.environ['SESSIONS_FILE'])
sid = room_state.get_session(os.environ['SESSIONS_FILE'])
sid_short = (sid[:8] + '…') if sid else '(none)'

local_time = time.strftime('%H:%M %Z')

state_str = render_state(spawn_state)

if mode == '/ping':
    # Pack the rich state into the one-line ping format.
    print(f"pong • {state_str} • Q={queue} • effort={effort} • model={model} • up {uptime} • {local_time}")
else:
    room_label = os.environ.get('ROOM_LABEL', '').strip()
    room_id = os.environ.get('ROOM_CHAT_ID', '?')
    room_display = f"{room_label} ({room_id})" if room_label else room_id
    # Show wrapper liveness AND turn state as separate facts (they're
    # different concerns: wrapper dead = bug, idle = normal waiting).
    wrapper_line = (f"yes (pid={busy_pid})" if wrapper_alive
                    else "no (no spawn process)")
    lines = [
        f"**Status** @ {local_time}",
        f"  Bot uptime:   {uptime}",
        f"  Room:         {room_display}",
        f"  Spawn alive:  {wrapper_line}",
        f"  State:        {state_str}",
        f"  Queue depth:  {queue}",
        f"  Effort:       {effort}",
        f"  Model:        {model}",
        f"  Active cwd:   {cwd}",
        f"  Session:      {sid_short}",
    ]
    if last_spawn:
        lines.append(f"  Last spawn:    {last_spawn}")

    # ---- last N session events from claude's transcript ----
    # The transcript file lives at:
    #   ~/.claude/projects/<flat-cwd>/<session_uuid>.jsonl
    # where <flat-cwd> is the cwd with slashes turned to dashes.
    # We tail the last few meaningful events (assistant text, tool_use,
    # real user messages, NOT tool_result rows or system noise) so the
    # user can see "what claude is doing right now, and for how long"
    # without having to dig into the chat themselves.
    EVENT_LIMIT = 5
    if sid and cwd:
        flat = cwd.replace('/', '-')
        tx_path = Path.home() / ".claude" / "projects" / flat / f"{sid}.jsonl"
        if tx_path.exists():
            events = []
            try:
                with open(tx_path) as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue
                        formatted = format_event(ev)
                        if formatted:
                            events.append(formatted)
            except Exception:
                events = []
            if events:
                lines.append("")
                lines.append(f"Last {min(EVENT_LIMIT, len(events))} session events:")
                tail = events[-EVENT_LIMIT:]
                now = datetime.now(timezone.utc)
                prev_ts = None
                for label, ts in tail:
                    delta = ""
                    if ts:
                        if prev_ts:
                            d = (ts - prev_ts).total_seconds()
                            delta = f" (+{humanize(d)})"
                        prev_ts = ts
                        local_t = ts.astimezone().strftime('%H:%M:%S')
                    else:
                        local_t = "      ?"
                    lines.append(f"  {local_t}{delta}  {label}")
                if prev_ts:
                    age = (now - prev_ts).total_seconds()
                    lines.append(f"  ↳ last event was {humanize(age)} ago")

    print('\n'.join(lines))
PYEOF
