# commands/menu.sh - handler for /menu|/start
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # The MENU is the primary interface. Slash commands are still wired
    # but only /menu is registered with setMyCommands so the input bar's
    # `/` autocomplete shows just one entry. Everything else lives behind
    # buttons.
    #
    # Live state is rendered into both the header AND the button labels
    # so a glance at the menu shows current cwd / effort / model / busy
    # without drilling.
    #
    # MENU_CLOSE_VERB / MENU_CLOSE_LABEL let a caller override the bottom
    # row's button (default "▾ collapse" -> act:close, which shrinks this
    # SAME message down to a stub instead of deleting it, see close.sh).
    # menu_new.sh (the turn-end footer's "☰ Menu" button, which always
    # sends a FRESH message rather than editing the footer, see its own
    # docstring) sets these to "✕ close" -> act:close_delete instead: a
    # footer-spawned menu is a one-off lookup, not a home base, and the
    # ordinary footer button is still sitting right above it as the real
    # permanent entry point, so collapsing to a second stub here would
    # just be a duplicate "☰ Menu" that never goes away.

    # First-time menu open per room: attach a persistent reply keyboard
    # with a single "📋 Menu" button above the text input so the user has
    # a one-tap way to reopen the menu from anywhere. Telegram remembers
    # the reply keyboard per chat (once set, every subsequent message
    # leaves it in place), so we only need to fire this once per room.
    # selective=true in tg.sh scopes the keyboard to the triggering user
    # in group chats; in DMs there's only one user so it's a no-op.
    if [[ ! -f "$ROOM_DIR/.reply_kb_attached" ]]; then
      CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" show_reply_kb \
        "📋 Menu shortcut: tap the button below (or type \`m\` / \`menu\`) anytime to reopen this." \
        "${CALLBACK_MSG_ID:-}" >/dev/null 2>&1 || true
      touch "$ROOM_DIR/.reply_kb_attached"
    fi
    MENU_OUT=$(REPO_ROOT="$REPO_ROOT" ROOM_DIR="$ROOM_DIR" \
               ROOM_CHAT_ID="$ROOM_CHAT_ID" \
               MENU_CLOSE_VERB="${MENU_CLOSE_VERB:-close}" \
               MENU_CLOSE_LABEL="${MENU_CLOSE_LABEL:-▾ collapse}" \
               python3 <<'PYEOF'
import json, os, time
from pathlib import Path

rd = Path(os.environ['ROOM_DIR'])
repo = Path(os.environ['REPO_ROOT'])

def read(p, default=""):
    try:
        return Path(p).read_text().strip() or default
    except Exception:
        return default

label = read(rd / "label", "this room")
effort = read(rd / "effort", "default")
model = read(rd / "model", "default")
import sys as _sys
_sys.path.insert(0, str(repo / "scripts"))
import room_state  # single source of truth for sessions.json
_sf = str(rd / "sessions.json")
cwd_full = room_state.get_cwd(_sf)
sid = room_state.get_session(_sf)
cwd_short = cwd_full.split("/")[-1] or "(none)"
sid_short = (sid[:8] + "…") if sid else "(fresh)"

# Real turn state from spawn_state.py: the SAME source /status uses. The old
# busy.lock-pid check reported "busy" forever in persistent mode, because the
# `tail -f | claude` spawn stays alive between turns waiting for the next
# message; its pid being alive does NOT mean claude is working. That's exactly
# why the menu said "busy" while /status (correctly) said "idle". "busy" now
# means claude is actively streaming / running a tool / awaiting a result.
import subprocess as _sp
try:
    _sj = _sp.check_output(
        ["python3", str(repo / "scripts" / "spawn_state.py"), str(rd)],
        text=True, timeout=3)
    _state = (json.loads(_sj) or {}).get("state", "no_spawn")
except Exception:
    _state = "no_spawn"
busy = _state in ("streaming", "tool", "awaiting_result")

# Pretty labels: show CURRENT values so the menu doubles as a status panel.
# Config-rare actions (rooms, tab, schedule, label, verbosity, users, plugin
# update, login, alerts, restart, mentions) live behind "⚙️ more options"
# (commands/more.sh) so the main menu carries only the daily controls.
b_status = "📊 status" + (" • 🟠 busy" if busy else " • 💤 idle")
b_usage = "📈 usage"
b_cd = f"📁 cwd: {cwd_short}"
b_new = f"🔄 new ({sid_short})"
# Combined model+effort entry: one button, one screen with both sections
# (commands/tune.sh). Drop the redundant "claude-" prefix in the preview.
_model_disp = model[7:] if model.startswith("claude-") else model
b_tune = f"🧠 {_model_disp} · ⚡ {effort}"
b_windows = "🪟 windows"
b_cancel = "🛑 cancel" if busy else "🛑 cancel (nothing running)"
b_help = "❓ help"

# Two-column grid. Telegram caps button text at ~64 chars; we're well under.
rows = [
    [{"text": b_status,     "callback_data": "act:status"},
     {"text": b_usage,      "callback_data": "act:usage"}],
    [{"text": b_cd,         "callback_data": "act:cd"},
     {"text": b_new,        "callback_data": "act:new"}],
    [{"text": b_tune,       "callback_data": "act:tune"}],
    [{"text": b_windows,    "callback_data": "act:windows"},
     {"text": "🗜 compact", "callback_data": "act:compact"},
     {"text": b_cancel,     "callback_data": "act:cancel"}],
    [{"text": "⚙️ more options", "callback_data": "act:more"},
     {"text": b_help,       "callback_data": "act:help"}],
    [{"text": os.environ["MENU_CLOSE_LABEL"],
      "callback_data": "act:" + os.environ["MENU_CLOSE_VERB"]}],
]

header = f"**🎯 {label}** • _{cwd_full or '(no cwd)'}_"
print(header + "\x1f" + json.dumps(rows))
PYEOF
)
    HEAD="${MENU_OUT%%$'\x1f'*}"
    KB="${MENU_OUT#*$'\x1f'}"
    # with_back=0: the menu IS the destination, a back button would
    # point to itself.
    reply_kb "$HEAD" "$KB" 0
