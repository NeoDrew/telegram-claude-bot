# commands/plugin.sh - handler for the "plugin" action.
#
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, ROOM_CHAT_ID, CMD, ARGS,
# CALLBACK_MSG_ID and lib.sh helpers (reply_kb, ...) are all in scope.
#
# Modes:
#   plugin update-all      → enumerate installed plugins via
#                            `claude plugin list`, run `claude plugin update`
#                            on each, post a summary (updated to vX /
#                            already latest / failed).
#   plugin update <name>   → update a single plugin by full id
#                            (name@marketplace). Kept for future uses;
#                            the menu fires update-all.
#
# Why this lives in the bot: updating a plugin via `/plugins` in
# interactive Claude Code requires a TUI the bot doesn't have. The
# `claude plugin` CLI is the non-interactive path. Plugins (and any MCP
# server child processes they spawn, e.g. chromeflow) load at process
# start, so the update only takes effect once the running persistent
# claude process is replaced by a new one, and a persistent room's
# process does NOT replace itself just because a message arrives (that's
# the whole point of persistent mode: it stays resident between turns,
# reading the next line off the inbox). Relying on "send any message" to
# eventually trigger a fresh spawn was wrong for exactly the rooms most
# likely to test a fresh plugin update: a long-lived, currently-busy
# session whose old process (and its old MCP server children, still
# reading the pre-update plugin cache) just keeps running: the plugin
# cache updates correctly on disk, but the room's existing claude process,
# and its already-spawned MCP server children pinned to the stale cache
# path, are untouched by the update and keep answering with old code.
#
# Fix: explicitly kill the room's current spawn (if any) the moment the
# update succeeds, via the same "stale" path listener.sh's own automatic
# stale-spawn-refresh uses (kill_room_spawn ... stale); session_id is
# preserved, no crash-loop stamp, no visible "cancelled" badge. The next
# message then genuinely starts a fresh process (and fresh MCP server
# children) against whatever is on disk right now, no ambiguity.
#
# Caveat: `claude plugin update` returns exit 0 even on "Plugin not
# found" errors, printing a "✘ Failed to update" line instead. We
# inspect stdout to flip the result so the user actually sees failures.

    SUBCMD=$(echo "$ARGS" | awk '{print tolower($1)}')

    # Resolve the claude binary the spawn uses. CLAUDE_BIN env override
    # wins, else the standard install path, else fall back to $PATH.
    CLAUDE_BIN="${CLAUDE_BIN:-$(claude_bin)}"
    if [[ ! -x "$CLAUDE_BIN" ]]; then
      ALT=$(command -v claude 2>/dev/null || true)
      if [[ -n "$ALT" ]]; then
        CLAUDE_BIN="$ALT"
      else
        reply_kb "❌ claude binary not found at \`$CLAUDE_BIN\` and not on PATH." "[]"
        exit 0
      fi
    fi

    # Classify a single `claude plugin update` invocation's output into
    # one of three states: updated / already-latest / failed. Writes the
    # per-plugin result line to stdout.
    classify_update() {
      local plugin="$1"
      local out="$2"
      if echo "$out" | grep -qE "^✘|Failed to update"; then
        # Take the first ✘ line as the user-visible reason.
        local reason
        reason=$(echo "$out" | grep -E "^✘|Failed to update" | head -1 \
                 | sed -E 's/^✘[[:space:]]*//; s/^.*Failed to update plugin \"[^\"]+\":[[:space:]]*//')
        echo "❌ \`$plugin\`: ${reason:-unknown error}"
      elif echo "$out" | grep -q "already at the latest"; then
        local ver
        ver=$(echo "$out" | grep -oE '\([0-9]+\.[0-9]+\.[0-9]+\)' | head -1 | tr -d '()')
        echo "✓ \`$plugin\` (${ver:-latest})"
      else
        # Successful update: try to extract the new version number.
        local ver
        ver=$(echo "$out" | grep -oE '(to |→ |version )[0-9]+\.[0-9]+\.[0-9]+' \
              | head -1 | sed -E 's/^(to |→ |version )//')
        echo "⬆ \`$plugin\` → ${ver:-updated}"
      fi
    }

    # Kill the room's current persistent spawn (if any) so the next
    # message genuinely starts fresh against the just-updated plugin
    # cache, instead of silently reusing an already-running process (and
    # its already-spawned MCP server children) that predates the update.
    # Returns the right trailing sentence for either case.
    restart_note_after_update() {
      if is_busy "$ROOM_CHAT_ID"; then
        kill_room_spawn "$ROOM_DIR" stale
        echo "_Current session stopped so the next message reconnects with the updated plugin code (conversation preserved)._"
      else
        echo "_Next message starts a fresh process with the updated plugin code._"
      fi
    }

    if [[ "$SUBCMD" == "update-all" ]]; then
      reply_kb "⏳ Listing installed plugins…" "[]"

      # Plugin ids look like "name@marketplace" in the `plugin list`
      # output. Anything that matches that shape on a "❯ "-led line is
      # a real installed plugin; lines without "@" are headers/blanks.
      LIST_OUT=$("$CLAUDE_BIN" plugin list 2>&1)
      # mapfile is bash 4+, macOS ships bash 3.2, so read into the array
      # via a while loop instead.
      PLUGINS=()
      while IFS= read -r _p; do
        [[ -n "$_p" ]] && PLUGINS+=("$_p")
      done < <(printf '%s\n' "$LIST_OUT" \
               | grep -oE '❯ [^[:space:]]+@[^[:space:]]+' \
               | sed 's/^❯ //')

      if [[ ${#PLUGINS[@]} -eq 0 ]]; then
        reply_kb "**🧩 No plugins installed**

\`\`\`
$LIST_OUT
\`\`\`" "[]"
        exit 0
      fi

      reply_kb "⏳ Updating ${#PLUGINS[@]} plugins…
$(printf '• %s\n' "${PLUGINS[@]}")" "[]"

      RESULTS=()
      FAIL_COUNT=0
      UPDATE_COUNT=0
      for plugin in "${PLUGINS[@]}"; do
        OUT=$("$CLAUDE_BIN" plugin update "$plugin" 2>&1)
        line=$(classify_update "$plugin" "$OUT")
        RESULTS+=("$line")
        case "$line" in
          ❌*) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
          ⬆*)  UPDATE_COUNT=$((UPDATE_COUNT + 1)) ;;
        esac
      done

      # Header summary line so the user can see the outcome at a glance.
      if [[ "$FAIL_COUNT" -gt 0 ]]; then
        HEADER="**🧩 Plugin update: $FAIL_COUNT failed, $UPDATE_COUNT updated**"
      elif [[ "$UPDATE_COUNT" -gt 0 ]]; then
        HEADER="**🧩 Plugin update: ✅ $UPDATE_COUNT updated**"
      else
        HEADER="**🧩 Plugin update: ✓ everything already at latest**"
      fi

      BODY="$HEADER

$(printf '%s\n' "${RESULTS[@]}")"

      if [[ "$UPDATE_COUNT" -gt 0 ]]; then
        BODY+="

$(restart_note_after_update)"
      fi
      reply_kb "$BODY" "[]"
      exit 0
    fi

    if [[ "$SUBCMD" == "update" ]]; then
      PLUGIN_NAME=$(echo "$ARGS" | awk '{print $2}')
      if [[ -z "$PLUGIN_NAME" ]]; then
        reply_kb "Usage: plugin update <name@marketplace> | plugin update-all" "[]"
        exit 0
      fi
      reply_kb "⏳ Updating plugin \`$PLUGIN_NAME\`…" "[]"
      OUT=$("$CLAUDE_BIN" plugin update "$PLUGIN_NAME" 2>&1)
      if [[ ${#OUT} -gt 1500 ]]; then
        OUT="${OUT:0:1500}
…(truncated)"
      fi
      line=$(classify_update "$PLUGIN_NAME" "$OUT")
      case "$line" in
        ❌*)
          reply_kb "**$line**

\`\`\`
$OUT
\`\`\`" "[]"
          ;;
        ⬆*)
          reply_kb "**$line**

\`\`\`
$OUT
\`\`\`

$(restart_note_after_update)" "[]"
          ;;
        *)
          reply_kb "**$line**

\`\`\`
$OUT
\`\`\`" "[]"
          ;;
      esac
      exit 0
    fi

    reply_kb "Usage: plugin update <name@marketplace> | plugin update-all" "[]"
