# commands/model.sh - handler for /model
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Set or read the Claude model for every subsequent spawn in this room.
    # Backed by state/rooms/<room>/model. spawn_claude.sh reads it.
    MODEL_FILE="$MODEL_FILE"

    # Looks up the alias in model_effort.py's OPTIONS, the SAME list the
    # model buttons are generated from below, instead of a separately
    # hand-maintained bash table. A duplicate table drifts: when OPTIONS
    # split "sonnet 5" (alias sonnet5) from "sonnet 4.6" (alias sonnet46),
    # a stale case statement that only knew the bare "sonnet" alias sent
    # both sonnet buttons to the unknown-model error.
    # "opus48" is kept as a typed-only synonym for opus 4.8 (not offered
    # on any button, no OPTIONS entry to drift from). Bare "opus" tracks
    # whatever DEFAULT_MODEL currently is, so it falls through to the
    # python lookup below instead of being hardcoded here.
    resolve_model_alias() {
      case "$1" in
        opus48) echo "claude-opus-4-8"; return ;;
        opus) REPO_ROOT="$REPO_ROOT" python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import DEFAULT_MODEL
print(DEFAULT_MODEL)
"; return ;;
      esac
      REPO_ROOT="$REPO_ROOT" ALIAS="$1" python3 <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import OPTIONS

alias = os.environ.get('ALIAS', '')
for _label, a, full in OPTIONS:
    if a == alias:
        print(full)
        break
else:
    print(alias)
PYEOF
    }

    if [[ -z "$ARGS" ]]; then
      # Active model marked by ✅ on its button; the header is just the title.
      CUR=""
      [[ -f "$MODEL_FILE" && -s "$MODEL_FILE" ]] && CUR=$(tr -d '\n\r ' < "$MODEL_FILE")
      KB=$(CUR="$CUR" REPO_ROOT="$REPO_ROOT" python3 <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import OPTIONS as options  # shared model catalogue
from model_effort import default_label

cur = os.environ.get('CUR', '')
# opus 4.8 is offered only in its 1M-context form.
def btn(label, alias, full):
    text = f"✅ {label}" if cur == full else label
    return {"text": text, "callback_data": f"act:model {alias}"}
rows = [
    [btn(*options[0]), btn(*options[1])],
    [btn(*options[2]), btn(*options[3])],
    [btn(*options[4]), btn(*options[5])],
    [btn(*options[6])],
    # "default" wins the checkmark if no per-room override is set.
    [{"text": ("✅ " if not cur else "") + f"default ({default_label()})",
      "callback_data": "act:model default"}],
]
print(json.dumps(rows), end='')
PYEOF
)
      reply_kb "**🧠 Model**" "$KB"
      exit 0
    fi

    RAW=$(echo "$ARGS" | awk '{print tolower($1)}')
    CUR_MODEL=""
    [[ -f "$MODEL_FILE" && -s "$MODEL_FILE" ]] && CUR_MODEL=$(tr -d '\n\r ' < "$MODEL_FILE")

    # Effort levels are model-specific (Haiku has none, ultracode is
    # 4.8-tier only), so an actual model CHANGE resets the room's effort
    # to default. Re-selecting the current model is a no-op and keeps it.
    EFFORT_NOTE=""
    reset_effort_for_model_change() {
      if [[ -f "$EFFORT_FILE" ]]; then
        rm -f "$EFFORT_FILE"
        EFFORT_NOTE=" Effort reset to default (levels are model-specific)."
      fi
    }

    if [[ "$RAW" == "default" || "$RAW" == "none" || "$RAW" == "reset" ]]; then
      [[ -n "$CUR_MODEL" ]] && reset_effort_for_model_change
      rm -f "$MODEL_FILE"
      DEF_MODEL=$(REPO_ROOT="$REPO_ROOT" python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import DEFAULT_MODEL
print(DEFAULT_MODEL)
")
      echo "Model cleared. Next spawn uses ${DEF_MODEL} (default).${EFFORT_NOTE}"
      exit 0
    fi

    MODEL=$(resolve_model_alias "$RAW")
    if [[ "$MODEL" != claude-* ]]; then
      ALIAS_LIST=$(REPO_ROOT="$REPO_ROOT" python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import OPTIONS
print(', '.join(a for _l, a, _f in OPTIONS) + ', opus48')
")
      echo "Unknown model '$ARGS'. Use an alias or full claude-* model ID.
Aliases: $ALIAS_LIST
Examples: /model fable, /model claude-opus-4-8[1m], /model default"
      exit 0
    fi
    [[ "$MODEL" != "$CUR_MODEL" ]] && reset_effort_for_model_change
    echo "$MODEL" > "$MODEL_FILE"
    # A running spawn is never stopped for this: listener.sh's stale-spawn
    # check sees the newer model file and restarts it (session kept) at the
    # next message sent between turns.
    if is_busy "$ROOM_CHAT_ID"; then
      echo "Model set to $MODEL. It takes over from your next message; anything running now finishes first.${EFFORT_NOTE}"
    else
      echo "Model set to $MODEL for this room.${EFFORT_NOTE}"
    fi
