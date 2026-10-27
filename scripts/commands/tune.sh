# commands/tune.sh - combined model + effort screen (one menu button).
# SOURCED by handle_action.sh: REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, ARGS,
# EFFORT_FILE, MODEL_FILE and lib.sh helpers are in scope.
#
# One screen, two sections. The effort section is gated to what the
# CURRENT model supports (matrix in scripts/model_effort.py), so tapping
# a model re-renders the screen with that model's effort options. Setting
# a model goes through the real `model` handler (subprocess re-dispatch),
# which keeps its semantics: a model CHANGE resets effort to default, a
# same-model tap is a no-op. Same for `effort`.
#
#   tune                  → render the combined screen
#   tune model <alias>    → set model, re-render
#   tune effort <level>   → set effort, re-render

    SUB=$(echo "$ARGS" | awk '{print tolower($1)}')
    REST=$(echo "$ARGS" | cut -d' ' -f2-)
    [[ "$REST" == "$ARGS" ]] && REST=""

    if [[ ( "$SUB" == "model" || "$SUB" == "effort" ) && -n "$REST" ]]; then
      # Re-dispatch to the real handler for the state change (output
      # discarded; the re-rendered screen below shows the result).
      CHAT_ID="$ROOM_CHAT_ID" bash "$REPO_ROOT/scripts/handle_action.sh" "$SUB $REST" >/dev/null 2>&1
    fi

    CUR_MODEL=""
    [[ -f "$MODEL_FILE" && -s "$MODEL_FILE" ]] && CUR_MODEL=$(tr -d '\n\r ' < "$MODEL_FILE")
    CUR_EFFORT=""
    [[ -f "$EFFORT_FILE" && -s "$EFFORT_FILE" ]] && CUR_EFFORT=$(tr -d '\n\r ' < "$EFFORT_FILE")

    KB=$(CUR_MODEL="$CUR_MODEL" CUR_EFFORT="$CUR_EFFORT" REPO_ROOT="$REPO_ROOT" python3 <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_ROOT'], 'scripts'))
from model_effort import OPTIONS, DEFAULT_MODEL, levels_for, display, default_label

cur_model = os.environ.get('CUR_MODEL', '')
cur_effort = os.environ.get('CUR_EFFORT', '')
effective_model = cur_model or DEFAULT_MODEL

rows = [[{"text": "· 🧠 model ·", "callback_data": "decor:tune"}]]

def mbtn(label, alias, full):
    text = f"✅ {label}" if cur_model == full else label
    return {"text": text, "callback_data": f"act:tune model {alias}"}

rows.append([mbtn(*OPTIONS[0]), mbtn(*OPTIONS[1])])
rows.append([mbtn(*OPTIONS[2]), mbtn(*OPTIONS[3])])
rows.append([mbtn(*OPTIONS[4]), mbtn(*OPTIONS[5])])
rows.append([mbtn(*OPTIONS[6])])
rows.append([{"text": ("✅ " if not cur_model else "") + f"default ({default_label()})",
              "callback_data": "act:tune model default"}])

rows.append([{"text": f"· ⚡ effort for {display(effective_model)} ·",
              "callback_data": "decor:tune"}])

levels, ultra = levels_for(effective_model)

def ebtn(label, cmd):
    sel = (label == cur_effort) or (label == "default" and not cur_effort)
    return {"text": (f"✅ {label}" if sel else label),
            "callback_data": f"act:tune effort {cmd}"}

if not levels:
    rows.append([{"text": "ℹ️ this model has no effort levels",
                  "callback_data": "decor:tune"}])
else:
    rows.append([ebtn(l, l) for l in levels[:3]])
    if len(levels) > 3:
        rows.append([ebtn(l, l) for l in levels[3:]])
    if ultra:
        rows.append([ebtn("ultracode", "ultracode")])
rows.append([ebtn("default", "default")])

print(json.dumps(rows), end='')
PYEOF
)
    reply_kb "**🧠⚡ Model + effort** · changing model resets effort" "$KB"
