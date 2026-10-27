# commands/windows.sh - handler for /windows|/screenshot
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Capture EVERY visible user window via Quartz and send them as
    # Telegram album(s), so you can see and audit the whole screen from
    # your phone. capture_window.py reads each window's offscreen buffer,
    # so this works even when the Mac's display is off or asleep,
    # unlike `screencapture`, which reads the rendered framebuffer and
    # returns black/empty images (that was the old "...error").
    #
    # The one-line follow-up is emitted AFTER the photo uploads so the
    # listener's "wrap every text reply with back-to-menu" path attaches a
    # back button to the morphed source message; the photos arrive as
    # separate messages below it.
    VENV_PY="$REPO_ROOT/.venv/bin/python3"
    if [[ ! -x "$VENV_PY" ]]; then
      echo "🪟 Window screenshots aren't set up on the Mac yet. Running scripts/install.sh there once enables them."
      exit 0
    fi

    WORKDIR="${TMPDIR:-/tmp}/tgclaude_windows_$$"
    mkdir -p "$WORKDIR"

    # Single log for the whole /windows lifecycle: enumerate, capture (with
    # the tool's real stderr, which is the only thing that says WHY a capture
    # returned nothing), send, and a one-line summary. Without this the
    # capture step is a black hole and we're back to guessing.
    WIN_LOG="$REPO_ROOT/logs/windows.log"
    echo "== /windows $(date -u +%FT%TZ) chat=${CHAT_ID:-?} ==" >>"$WIN_LOG" 2>&1

    # Enumerate visible windows, capture each. SHOTS holds "path::caption"
    # pairs in the exact format tg.sh media_group expects.
    declare -a SHOTS=()
    IDX=0
    ENUM=0
    while IFS=$'\t' read -r WID OWNER TITLE; do
      [[ -z "$WID" ]] && continue
      ENUM=$((ENUM + 1))
      OUT="$WORKDIR/win_${IDX}.png"
      # Quartz primary (works with the monitor off); screencapture -l
      # fallback mirrors /tab for contexts where Quartz returns nothing.
      # Both tools' stderr is kept (the reason a capture failed lives there).
      # /usr/sbin/screencapture by absolute path: a launchd PATH that omits
      # /usr/sbin makes the bare command name fail as "command not found",
      # which would leave this fallback unreachable whenever the Quartz
      # primary path also failed, exactly the case it exists to cover.
      if { "$VENV_PY" "$REPO_ROOT/scripts/capture_window.py" "$WID" "$OUT" 2>>"$WIN_LOG" && [[ -s "$OUT" ]]; } \
         || { /usr/sbin/screencapture -l "$WID" -x -o -t png "$OUT" 2>>"$WIN_LOG" && [[ -s "$OUT" ]]; }; then
        CAP="$OWNER"
        [[ -n "$TITLE" ]] && CAP="$OWNER: $TITLE"
        # media_group splits each arg on its first "::" into path::caption,
        # so neutralise any "::" the window title might contain.
        CAP="${CAP//::/-}"
        SHOTS+=("$OUT::$CAP")
        IDX=$((IDX + 1))
      else
        echo "capture FAILED wid=$WID owner='$OWNER' title='$TITLE'" >>"$WIN_LOG"
      fi
    done < <("$VENV_PY" "$REPO_ROOT/scripts/enum_windows.py" 2>>"$WIN_LOG")

    N=${#SHOTS[@]}
    if [[ "$N" -eq 0 ]]; then
      echo "summary enumerated=$ENUM captured=0 sent=0" >>"$WIN_LOG"
      rm -rf "$WORKDIR"
      echo "🪟 No windows captured. Either nothing is open, or the Mac hasn't given the bot Screen Recording permission (System Settings, Privacy & Security, Screen Recording)."
      exit 0
    fi

    # Send in batches of up to 10 (Telegram's media-group cap). A lone
    # leftover goes via sendPhoto, which media_group can't do (needs ≥2).
    #
    # sendMediaGroup is ATOMIC: if any one photo in the group is rejected
    # (Telegram caps each side at 10000px, width+height at 10000, and the
    # aspect ratio at 20:1, so a single odd-shaped or oversized capture
    # poisons the whole group), or the multipart upload fails, Telegram
    # drops the WHOLE group and all N photos fail together. That is how one
    # bad capture among several turns into "Sent 0 windows (of 7 captured)".
    # So when the group call fails we fall back to per-photo sends, which are
    # judged independently and let the healthy photos through.
    #
    # We DON'T discard tg.sh's stderr any more: the real Telegram API error
    # is the only thing that says WHY a send failed (bad dimensions vs a
    # network drop vs anything else), so it goes to logs/windows.log (defined
    # at the top of this handler). Without it the failure reason is lost and
    # we're left guessing.
    SENT=0
    send_one() {  # path::caption -> increments SENT on success
      local pair="$1" p c
      p="${pair%%::*}"; c="${pair#*::}"
      if "$REPO_ROOT/scripts/tg.sh" photo "$p" "$c" >>"$WIN_LOG" 2>&1; then
        SENT=$((SENT + 1))
      fi
    }
    declare -a BATCH=()
    send_batch() {
      local n="${#BATCH[@]}" pair
      if [[ "$n" -eq 0 ]]; then
        return 0
      elif [[ "$n" -eq 1 ]]; then
        send_one "${BATCH[0]}"
      elif "$REPO_ROOT/scripts/tg.sh" media_group "${BATCH[@]}" >>"$WIN_LOG" 2>&1; then
        SENT=$((SENT + n))
      else
        # Atomic group failed; salvage what we can one photo at a time.
        for pair in "${BATCH[@]}"; do send_one "$pair"; done
      fi
      BATCH=()
    }

    for pair in "${SHOTS[@]}"; do
      BATCH+=("$pair")
      [[ "${#BATCH[@]}" -eq 10 ]] && send_batch
    done
    send_batch

    echo "summary enumerated=$ENUM captured=$N sent=$SENT" >>"$WIN_LOG"
    rm -rf "$WORKDIR"
    if [[ "$SENT" -eq "$N" ]]; then
      if [[ "$SENT" -eq 1 ]]; then echo "🪟 Sent 1 window."; else echo "🪟 Sent $SENT windows."; fi
    elif [[ "$SENT" -eq 0 ]]; then
      # All N captured fine on disk but Telegram accepted none. We don't
      # assert a cause here (the real Telegram error is in logs/windows.log);
      # a bad-dimension capture, an oversized photo, and a network drop all
      # land here, so a guessed cause would often be wrong.
      echo "🪟 Captured $N windows, but Telegram didn't accept the uploads. Try again in a moment."
    else
      echo "🪟 Sent $SENT of $N windows; Telegram didn't accept the rest. Try again for the missing ones."
    fi
