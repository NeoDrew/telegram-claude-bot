# commands/compact.sh - inject /compact into the active spawn to compress context.
# SOURCED by handle_action.sh (not executed standalone): REPO_ROOT, ROOM_DIR,
# ROOM_CHAT_ID, BUSY_LOCK, QUEUE_FILE and lib.sh helpers are all in scope.

    INBOX="$ROOM_DIR/inbox.ndjson"
    if [[ -f "$ROOM_DIR/.inbox_ready" ]]; then
      printf '{"type":"user","message":{"role":"user","content":"/compact"}}\n' >> "$INBOX"
      touch "$ROOM_DIR/.compact_in_flight"
      echo "🗜 Compressing context."
    elif is_busy "$ROOM_CHAT_ID"; then
      # Spawn is live but not accepting inbox messages (warmup/teardown window).
      # Queue as a marker so the next spawn injects /compact BEFORE the next
      # user message, rather than coalescing it with that message as plain text.
      touch "$ROOM_DIR/.compact_pending"
      echo "🗜 Queued context compression."
    else
      # Room is idle. Mark it; the next spawn will inject /compact first.
      touch "$ROOM_DIR/.compact_pending"
      echo "🗜 Context compression queued for next session."
    fi
