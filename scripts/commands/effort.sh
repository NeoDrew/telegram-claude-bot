# commands/effort.sh - handler for /effort
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Set or read the effort level passed to every subsequent claude spawn.
    # Backed by state/rooms/<room>/effort (single line). spawn_claude.sh reads it.
    # "default" (no per-room override) passes no --effort flag, so claude's
    # own configured effort level applies.
    # The bare-/effort keyboard gates the offered levels to what the CURRENT
    # model supports (matrix lives in the python block below). "ultracode" is
    # a Claude Code session setting, NOT an --effort value; spawn_claude.sh
    # translates a stored "ultracode" into --settings '{"ultracode":true}'.
    EFFORT_FILE="$EFFORT_FILE"
    VALID_LEVELS="low medium high xhigh max ultracode"

    if [[ -z "$ARGS" ]]; then
      # Bare /effort → show inline keyboard with the current selection
      # marked by ✅ on its button. The header is now just the title;
      # the active state is visible in the keyboard itself.
      CUR=""
      [[ -f "$EFFORT_FILE" && -s "$EFFORT_FILE" ]] && CUR=$(tr -d '\n\r ' < "$EFFORT_FILE")
      MODEL=""
      [[ -f "$MODEL_FILE" && -s "$MODEL_FILE" ]] && MODEL=$(tr -d '\n\r ' < "$MODEL_FILE")
      KB=$(CUR="$CUR" MODEL="$MODEL" REPO_ROOT="$REPO_ROOT" python3 <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import levels_for, DEFAULT_MODEL  # shared model→effort matrix

cur = os.environ.get('CUR', '')
# No per-room override: the room runs DEFAULT_MODEL.
model = os.environ.get('MODEL', '').strip() or DEFAULT_MODEL

levels, ultra = levels_for(model)

def b(label, cmd):
    sel = (label == cur) or (label == "default" and not cur)
    return {"text": (f"✅ {label}" if sel else label),
            "callback_data": f"act:effort {cmd}"}

rows = []
if not levels:
    # Haiku etc.: no effort knob. Say so; offer only the clear button.
    rows.append([{"text": "ℹ️ this model has no effort levels",
                  "callback_data": "decor:effort-none"}])
else:
    rows.append([b(l, l) for l in levels[:3]])
    if len(levels) > 3:
        rows.append([b(l, l) for l in levels[3:]])
    if ultra:
        rows.append([b("ultracode", "ultracode")])
rows.append([b("default", "default")])
print(json.dumps(rows), end='')
PYEOF
)
      # Effort options vary by model, so name the model in the header.
      _mlabel="${MODEL#claude-}"
      if [[ -z "$_mlabel" ]]; then
        _mlabel="default ($(REPO_ROOT="$REPO_ROOT" python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import default_label
print(default_label())
"))"
      fi
      reply_kb "**⚡ Effort level** · ${_mlabel}" "$KB"
      exit 0
    fi

    LEVEL=$(echo "$ARGS" | awk '{print tolower($1)}')
    if [[ "$LEVEL" == "default" || "$LEVEL" == "none" || "$LEVEL" == "reset" ]]; then
      rm -f "$EFFORT_FILE"
      echo "Effort cleared. Falls back to Claude Code's own default effort."
      exit 0
    fi

    OK=0
    for V in $VALID_LEVELS; do
      [[ "$LEVEL" == "$V" ]] && OK=1
    done
    if [[ $OK -eq 0 ]]; then
      echo "Invalid effort '$LEVEL'. Valid: $VALID_LEVELS, or 'default' to clear."
      exit 0
    fi

    echo "$LEVEL" > "$EFFORT_FILE"
    if [[ "$LEVEL" == "ultracode" ]]; then
      echo "⚡ Effort set to ultracode for this room. Sends xhigh AND lets $(bot_name) auto-orchestrate dynamic workflows for substantive tasks (Opus 4.8 / Fable tier). It can fan out many subagents, so it burns tokens fast. Takes effect on the next message."
    else
      echo "Effort set to $LEVEL for this room. Applies until changed or 'default' to clear."
    fi
