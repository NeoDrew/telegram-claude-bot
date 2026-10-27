# commands/usage.sh - handler for /usage
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    SUBARG=$(echo "$ARGS" | awk '{print tolower($1)}')

    if [[ "$SUBARG" == "refresh" ]]; then
      if [[ -n "${CALLBACK_MSG_ID:-}" ]]; then
        CHAT_ID="$ROOM_CHAT_ID" "$REPO_ROOT/scripts/tg.sh" edit \
          "$CALLBACK_MSG_ID" \
          "**📊 Claude Code usage**

🔄 _Refreshing from claude.ai/settings/usage… (~10s)_" >/dev/null 2>&1 || true
      fi
      "$REPO_ROOT/scripts/scrape_usage.sh" >/dev/null 2>&1
    fi

    # Reads state/usage_scrape.json (scraped from claude.ai, exact numbers) or
    # falls back to state/usage_cache.json (rate_limit_event stream data, only
    # has values once Anthropic starts reporting near a limit).
    USAGE_OUT=$(REPO_ROOT="$REPO_ROOT" python3 <<'PYEOF'
import json, time, os
from pathlib import Path
from datetime import datetime, timezone

now = int(time.time())

cache_path = Path(os.environ['REPO_ROOT']) / "state" / "usage_cache.json"
try:
    cache = json.loads(cache_path.read_text())
except Exception:
    cache = {}

scrape_path = Path(os.environ['REPO_ROOT']) / "state" / "usage_scrape.json"
try:
    scrape = json.loads(scrape_path.read_text())
except Exception:
    scrape = {}

def bar(util, width=20):
    if util is None:
        return f"`{'·' * width}` (under threshold)"
    pct = min(1.0, max(0.0, util))
    filled = int(round(pct * width))
    return f"`{'█' * filled}{'░' * (width - filled)}` {pct*100:.0f}%"

def fmt_reset(resets_at):
    if not resets_at: return "?"
    remaining = resets_at - now
    when_utc = datetime.fromtimestamp(resets_at, tz=timezone.utc).strftime('%H:%M UTC')
    if remaining <= 0:
        return f"_overdue_ (was {when_utc})"
    h = remaining // 3600
    m = (remaining % 3600) // 60
    when = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
    return f"in {h}h {m}m (at {when.strftime('%H:%M %Z')})"

def status_emoji(util):
    if (util or 0) >= 1: return '🔴'
    if (util or 0) >= 0.8: return '🟡'
    return '🟢'

PRETTY = {'five_hour': '5-hour session', 'weekly': 'Weekly',
          'weekly_opus': 'Weekly Opus', 'hourly': 'Hourly', 'seven_day': '7-day'}

lines = ["**📊 Claude Code usage**"]

STALE_SECS = 5 * 3600
fetched = scrape.get('fetched_at', 0) or 0
age = now - fetched

# Mode 1: fresh scrape from claude.ai
if scrape.get('meters') and age <= STALE_SECS:
    age_str = (f"{age}s ago" if age < 60 else
               f"{age // 60}m ago" if age < 3600 else
               f"{age // 3600}h ago")
    LABEL_PRETTY = {
        '5h': '5-hour session',
        'weekly_all': 'Weekly (all models)',
        'weekly_sonnet': 'Weekly (Sonnet)',
    }
    for m in scrape['meters']:
        pct = m.get('pct', 0)
        label = LABEL_PRETTY.get(m.get('label', ''), m.get('raw_label', m.get('label', '?')))
        util = pct / 100.0
        emoji = '🔴' if util >= 1 else '🟡' if util >= 0.8 else '🟢'
        resets = m.get('resets', '')
        lines.append("")
        lines.append(f"{emoji} **{label}**")
        lines.append(bar(util))
        if resets:
            lines.append(f"Resets {resets}")
    lines.append("")
    lines.append(f"_Scraped from claude.ai · {age_str}_")

# Mode 2: stream-derived cache from rate_limit_event (only reports near limits)
elif cache:
    for rtype, data in cache.items():
        util = data.get('utilization')
        ra = data.get('resetsAt')
        upd = data.get('updatedAt', 0)
        age2 = now - upd
        age_str = (f"{age2}s ago" if age2 < 60 else
                   f"{age2 // 60}m ago" if age2 < 3600 else
                   f"{age2 // 3600}h ago")
        emoji = status_emoji(util)
        lines.append("")
        lines.append(f"{emoji} **{PRETTY.get(rtype, rtype)}**")
        lines.append(bar(util))
        lines.append(f"Resets {fmt_reset(ra)}")
        lines.append(f"_updated {age_str}_")
        if data.get('isUsingOverage'):
            lines.append("⚠️ Using overage credits.")
    lines.append("")
    lines.append("_From Claude stream (only reports above ~80%). Tap 🔄 for exact numbers._")

# Mode 3: nothing yet
else:
    lines.append("")
    lines.append("_No data yet. Tap 🔄 Refresh to scrape claude.ai/settings/usage._")

KB = '[[{"text":"🔄 Refresh","callback_data":"act:usage refresh"},{"text":"× Dismiss","callback_data":"act:noop"}]]'
print('\n'.join(lines) + "\x1f" + KB)
PYEOF
)
      HEAD="${USAGE_OUT%%$'\x1f'*}"
      KB="${USAGE_OUT#*$'\x1f'}"
      reply_kb "$HEAD" "$KB"
