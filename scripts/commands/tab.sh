# commands/tab.sh - handler for /tab
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    # /tab: bring a Chrome tab to the front and screenshot it.
    #
    # The picker identifies tabs by Chrome's UNIQUE tab id, not by title, so
    # two tabs with the same title are selectable independently. (The old
    # picker fired "/tab <title>", and the substring search always activated
    # the FIRST tab containing that text, so duplicates were unreachable.)
    # Typing "/tab <text>" by hand still does a case-insensitive title/URL
    # substring search and grabs the first match.
    if [[ -z "$ARGS" ]]; then
      # Bare /tab: list every open Chrome tab as a button keyed by tab id.
      if ! pgrep -x "Google Chrome" >/dev/null 2>&1; then
        echo "Google Chrome is not running."
        exit 0
      fi
      TAB_LIST=$(osascript -e '
        tell application "Google Chrome"
          set out to ""
          repeat with w in (every window)
            repeat with t in (every tab of w)
              set out to out & (id of t) & "\t" & (title of t) & "\n"
            end repeat
          end repeat
          return out
        end tell
      ' 2>/dev/null)
      KB_OUT=$(TABS="$TAB_LIST" python3 <<'PYEOF'
import json, os
from collections import Counter
tabs = []
for line in (os.environ.get('TABS') or '').splitlines():
    if '\t' not in line:
        continue
    tid, title = line.split('\t', 1)
    tid, title = tid.strip(), title.strip()
    if tid:
        tabs.append((tid, title))
# Number duplicate titles (·1, ·2 …) so identical tabs are distinguishable.
counts = Counter(t for _, t in tabs)
seen = {}
rows = []
for tid, title in tabs[:15]:
    label = title or '(untitled)'
    if counts[title] > 1:
        seen[title] = seen.get(title, 0) + 1
        label = f"{label} ·{seen[title]}"
    rows.append([{"text": label[:60], "callback_data": f"act:tab id:{tid}"}])
if not rows:
    rows = [[{"text": "(no Chrome tabs found)", "callback_data": "decor:empty"}]]
print(json.dumps(rows))
PYEOF
)
      reply_kb "Pick a Chrome tab to screenshot:" "$KB_OUT"
      exit 0
    fi

    if ! pgrep -x "Google Chrome" >/dev/null 2>&1; then
      echo "Google Chrome is not running."
      exit 0
    fi

    # Selection. "id:<n>" (from a picker button) activates that exact tab by
    # its unique id; unambiguous even with duplicate titles. Anything else is
    # treated as a typed substring query against title + URL (first match).
    if [[ "$ARGS" == id:* ]]; then
      TAB_ID="${ARGS#id:}"
      TAB_ID="${TAB_ID//[^0-9]/}"   # digits only; guard the osascript input
      if [[ -z "$TAB_ID" ]]; then
        echo "Bad tab id."
        exit 0
      fi
      RESULT=$(TID="$TAB_ID" osascript -e '
        set targetId to system attribute "TID"
        tell application "Google Chrome"
          repeat with wi from 1 to (count of windows)
            set w to window wi
            set tabList to tabs of w
            repeat with ti from 1 to (count of tabList)
              set t to item ti of tabList
              if (id of t as string) is equal to targetId then
                set active tab index of w to ti
                set index of w to 1
                activate
                return "1::" & (title of t) & "::" & (URL of t)
              end if
            end repeat
          end repeat
          return "0::::"
        end tell
      ' 2>&1)
    else
      Q_LOWER=$(echo "$ARGS" | awk '{print tolower($0)}')
      RESULT=$(Q="$Q_LOWER" osascript -e '
        on tolower(s)
          set out to ""
          repeat with c in (every character of s)
            set asc to id of c
            if asc >= 65 and asc <= 90 then
              set out to out & (character id (asc + 32))
            else
              set out to out & c
            end if
          end repeat
          return out
        end tolower

        set q to system attribute "Q"
        tell application "Google Chrome"
          set matchCount to 0
          set foundTitle to ""
          set foundUrl to ""
          repeat with wi from 1 to (count of windows)
            set w to window wi
            set tabList to tabs of w
            repeat with ti from 1 to (count of tabList)
              set t to item ti of tabList
              set tt to my tolower(title of t)
              set tu to my tolower(URL of t)
              if (tt contains q) or (tu contains q) then
                set matchCount to matchCount + 1
                if matchCount = 1 then
                  set foundTitle to title of t
                  set foundUrl to URL of t
                  set active tab index of w to ti
                  set index of w to 1
                end if
              end if
            end repeat
          end repeat
          activate
          return (matchCount as string) & "::" & foundTitle & "::" & foundUrl
        end tell
      ' 2>&1)
    fi

    MATCH_N=$(echo "$RESULT" | awk -F"::" '{print $1}')
    MATCH_TITLE=$(echo "$RESULT" | awk -F"::" '{print $2}')
    MATCH_URL=$(echo "$RESULT" | awk -F"::" '{print $3}')

    if [[ -z "$MATCH_N" || "$MATCH_N" == "0" ]]; then
      if [[ "$ARGS" == id:* ]]; then
        echo "That tab is gone (closed since the list was built). Open /tab again."
      else
        echo "No Chrome tab matched '$ARGS'."
      fi
      exit 0
    fi

    # Give Chrome a beat to repaint and bring the matched window to front
    # before we capture (the window-title-as-tab-title swap is async).
    sleep 0.4

    # Find the CG window id of the Chrome window whose title matches the
    # active tab's title via the Quartz window enumerator. We just brought
    # the matched window to the front, so even with a duplicate title the
    # first (frontmost, z-order) match is ours.
    VENV_PY="$REPO_ROOT/.venv/bin/python3"
    if [[ ! -x "$VENV_PY" ]]; then
      echo "/tab needs the venv at .venv/. Run: python3 -m venv .venv && .venv/bin/pip install pyobjc-framework-Quartz"
      exit 0
    fi
    WID=$("$VENV_PY" "$REPO_ROOT/scripts/enum_windows.py" 2>/dev/null \
      | awk -F"\t" -v t="$MATCH_TITLE" '
          $2 == "Google Chrome" && $3 == t {print $1; exit}
        ')
    # Fallback: if Chrome's window title trims/decorates the tab title
    # (e.g. " - Google Chrome"), just take the frontmost Chrome window.
    if [[ -z "$WID" ]]; then
      WID=$("$VENV_PY" "$REPO_ROOT/scripts/enum_windows.py" 2>/dev/null \
        | awk -F"\t" '$2 == "Google Chrome" {print $1; exit}')
    fi
    if [[ -z "$WID" ]]; then
      echo "Matched tab '$MATCH_TITLE' but couldn't locate its window for capture."
      exit 0
    fi

    TS=$(date +%s)
    OUT="${TMPDIR:-/tmp}/tgclaude_tab_${TS}.png"
    # Quartz primary (works with monitor off); screencapture -l fallback.
    if ! "$VENV_PY" "$REPO_ROOT/scripts/capture_window.py" "$WID" "$OUT" 2>/dev/null || [[ ! -s "$OUT" ]]; then
      if ! screencapture -l "$WID" -x -o -t png "$OUT" 2>/dev/null || [[ ! -s "$OUT" ]]; then
        rm -f "$OUT"
        echo "Capture failed for Chrome window $WID. Both Quartz and screencapture returned empty. Check Screen Recording permission for the bot's launchd context."
        exit 0
      fi
    fi

    CAP="$MATCH_TITLE"
    if [[ -n "$MATCH_URL" ]]; then
      CAP="$CAP
$MATCH_URL"
    fi
    # Only the substring path can have multiple matches; the id path is exact.
    if [[ "$ARGS" != id:* && "$MATCH_N" -gt 1 ]]; then
      CAP="$CAP
(1 of $MATCH_N matches)"
    fi
    if "$REPO_ROOT/scripts/tg.sh" photo "$OUT" "$CAP" >/dev/null 2>&1; then
      rm -f "$OUT"
      # Follow-up text → listener attaches the back-to-menu button.
      echo "🔖 sent: $MATCH_TITLE"
      exit 0
    fi
    rm -f "$OUT"
    echo "(tg.sh photo upload failed for $OUT)"
