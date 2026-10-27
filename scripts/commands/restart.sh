# commands/restart.sh - one-tap listener restart from the menu.
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, CMD, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers (reply_kb, ...) are all in scope.
#
# Reloads the listener so on-disk changes go live (new gate logic, newly
# approved users, edited handlers that need a fresh listener process). This
# exists so the owner never needs a Terminal for the restart that listener.sh
# edits require; the button and this route are sourced fresh on every tap, so
# they work even BEFORE the pending restart they trigger.
#
# Why it can't kickstart synchronously: the listener is THIS handler's
# ancestor (handle_action.sh runs as a child of the listener). Killing it
# inline would take us down before the confirmation is sent and possibly
# before launchctl finishes. So: send the ack NOW (blocking), then fire a
# DETACHED, slightly-deferred restart. with_setsid.py puts it in its own
# session (macOS has no setsid(1)), so `kickstart -k` killing the listener's
# process group does not cascade to it; the small delay lets the ack land and
# this dispatch unwind first. launchd KeepAlive brings the listener back cold.

    # Ack synchronously (reply_kb blocks on the Telegram call) so the
    # user sees it before the listener dies, then schedule the detached restart.
    reply_kb "♻️ Restarting… I'll message here the moment I'm back up." '[]'

    # Marker so the freshly-booted listener announces "back up" to THIS room
    # (see announce_restart_if_marked in lib.sh). Written before the kickstart
    # so it's on disk when the new process reads it.
    printf '%s\n' "$ROOM_CHAT_ID" > "$REPO_ROOT/state/.restart_announce"
    emit_metric restart_manual

    # The actual restart command + delay are overridable so the offline test can
    # neutralise them (BOT_RESTART_CMD=recorder, BOT_RESTART_DELAY=0) and never
    # kickstart the real service. Default: the purpose-built safe_restart.sh.
    RESTART_CMD="${BOT_RESTART_CMD:-$REPO_ROOT/scripts/safe_restart.sh --force}"
    RESTART_DELAY="${BOT_RESTART_DELAY:-2}"
    # </dev/null + redirected stdio detaches the child from the command-
    # substitution pipe that invoke_action reads, so $(handle_action.sh ...)
    # returns immediately instead of blocking on this background process.
    python3 "$REPO_ROOT/scripts/with_setsid.py" \
      bash -c "sleep $RESTART_DELAY; exec $RESTART_CMD" \
      </dev/null >>"$REPO_ROOT/logs/listener.log" 2>&1 &
    disown 2>/dev/null || true
    exit 0
