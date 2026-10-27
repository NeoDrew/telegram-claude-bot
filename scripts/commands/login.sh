# commands/login.sh - handler for /login
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # Phone-driven OAuth rotation via login_watcher.py. The watcher
    # PTY-drives `claude setup-token` on the bot host, captures the
    # auth URL from its TUI fallback ("Browser didn't open? Use the
    # url below…"), posts it to Telegram. User taps from their phone,
    # authorizes, copies the code from Anthropic's redirect page,
    # pastes it back as a regular Telegram message. Listener forwards
    # the code through a FIFO to the watcher, which feeds it into
    # setup-token's PTY. Setup-token exchanges, prints the token, the
    # watcher rewrites .env atomically and kickstarts the listener.
    #
    # Detached via nohup + disown so the watcher survives the
    # kickstart at the end. Concurrency lock at state/.login_in_progress.
    LOCK="$REPO_ROOT/state/.login_in_progress"
    if [[ -f "$LOCK" ]]; then
      PID=$(cat "$LOCK" 2>/dev/null || true)
      if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        echo "🔐 Login flow already in progress (pid $PID). Wait for the URL; it may already be in another message above."
        exit 0
      fi
      rm -f "$LOCK"
    fi
    nohup python3 "$REPO_ROOT/scripts/login_watcher.py" "$ROOM_CHAT_ID" \
      >/dev/null 2>&1 </dev/null & disown
    echo "🔐 Starting login flow… you'll get a tap-to-authorize URL within ~5 seconds.

Tap the URL on your phone, approve, then paste the code Anthropic shows back here. The bot will exchange it, rotate the token, and restart itself."
