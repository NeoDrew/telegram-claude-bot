# commands/cancel.sh - handler for the "cancel" action.
#
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, SESSIONS_FILE,
# BUSY_LOCK, QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers (reply_kb, kill_room_spawn, is_busy)
# are all in scope.
#
# Works like pressing Esc in Claude Code: it interrupts the current turn
# and Claude moves straight on to the next message, nothing queued is
# dropped. The interrupt is a stream-json control request written to the
# live inbox; Claude ends the turn with an error_during_execution result,
# which the streamer reports as "Interrupted". If there is no live inbox
# (a silent one-shot, a spawn tearing down) or an earlier interrupt has
# gone unanswered for INTERRUPT_STUCK_S, the spawn is force-stopped
# instead, and every inbox line Claude had not read yet is moved to the
# queue so it is still answered. "cancel force" goes straight to the force
# stop; any other argument (older buttons send "cancel yes") is ignored.

    INTERRUPT_STUCK_S="${INTERRUPT_STUCK_S:-10}"
    IRQ="$ROOM_DIR/.interrupt_requested"
    irq_stuck=0
    if [[ -f "$IRQ" ]]; then
      irq_age=$(( $(date +%s) - $(stat -f %m "$IRQ" 2>/dev/null || date +%s) ))
      (( irq_age >= INTERRUPT_STUCK_S )) && irq_stuck=1
    fi

    if ! is_busy "$ROOM_CHAT_ID"; then
      rm -f "$BUSY_LOCK"
      reply_kb "Nothing running right now." "[]"
    elif [[ -f "$ROOM_DIR/.inbox_ready" && "$irq_stuck" == "0" && "${ARGS:-}" != "force" ]]; then
      cancel_state=$(python3 "$REPO_ROOT/scripts/spawn_state.py" "$ROOM_DIR" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
      if [[ "$cancel_state" == "idle" ]]; then
        reply_kb "Nothing running right now." "[]"
      else
        # Marker first: Claude can end the turn the instant the control
        # line lands, and the streamer reads the marker at that result.
        touch "$IRQ"
        # A control line, not a message: its .pending_reacts entry is []
        # so the two files stay line-for-line parallel.
        printf '{"type":"control_request","request_id":"cancel-%s","request":{"subtype":"interrupt"}}\n' \
          "$(date +%s)" >> "$ROOM_DIR/inbox.ndjson"
        echo '[]' >> "$ROOM_DIR/.pending_reacts"
        reply_kb "🛑 Interrupting. Anything you've queued will still be answered." "[]"
      fi
    else
      # Force stop. Keep every user line Claude had not read yet. The
      # first unanswered line is the turn being cancelled even when its
      # read echo has not been recorded yet.
      INBOX_F="$ROOM_DIR/inbox.ndjson" READ_F="$ROOM_DIR/.inbox_read" DONE_F="$ROOM_DIR/.turns_done" QUEUE_F="$QUEUE_FILE" REACTS_F="$ROOM_DIR/.pending_reacts" python3 - <<'PYEOF' 2>/dev/null
import json, os
def num(path):
    try:
        return int(open(path).read().strip() or 0)
    except Exception:
        return 0
read = max(num(os.environ["READ_F"]), num(os.environ["DONE_F"]) + 1)
try:
    lines = [l for l in open(os.environ["INBOX_F"]).read().splitlines() if l.strip()]
except Exception:
    lines = []
try:
    reacts = [l for l in open(os.environ["REACTS_F"]).read().splitlines() if l.strip()]
except Exception:
    reacts = []
with open(os.environ["QUEUE_F"], "a") as q:
    for i, line in enumerate(lines[read:], start=read):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        c = (obj.get("message") or {}).get("content") if obj.get("type") == "user" else None
        if isinstance(c, str) and c.strip() and c.strip() != "/compact":
            try:
                ids = [str(x) for x in json.loads(reacts[i]) if isinstance(x, int)]
            except Exception:
                ids = []
            # Ids ride after \x1f (listener.sh queue_line) so the reaction resolves.
            q.write(c.strip().replace("\n", "\x1e") + ("\x1f" + " ".join(ids) if ids else "") + "\n")
PYEOF
      kill_room_spawn "$ROOM_DIR" 1
      rm -f "$IRQ" "$ROOM_DIR/.inbox_read"
      reply_kb "🛑 Cancelled (force stopped). Moving on to the next message." "[]"
    fi
