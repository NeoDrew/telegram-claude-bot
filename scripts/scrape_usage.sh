#!/bin/bash
# scrape_usage.sh: fetch the real Claude Code subscription usage from
# claude.ai/settings/usage via a one-shot Haiku claude invocation that
# drives the browser through chromeflow. The result is JSON written to
# stdout (and cached to state/usage_scrape.json for /usage to read).
#
# chromeflow is an OPTIONAL browser plugin. Without it (or without a
# logged-in claude.ai tab in Chrome) the scrape fails and the cached JSON
# carries an "error" field instead of meters.
#
# Why Haiku: this is mechanical scraping (a tab switch, DOM extraction,
# JSON output) and needs no reasoning, so the smallest model is enough.
#
# Usage:
#   scrape_usage.sh           fetch, cache, print JSON
#   scrape_usage.sh --cache   print cached JSON without re-scraping

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="$REPO_ROOT/state/usage_scrape.json"

if [[ "${1:-}" == "--cache" ]]; then
  cat "$CACHE" 2>/dev/null || echo '{}'
  exit 0
fi

. "$REPO_ROOT/scripts/lib.sh"
TOKEN=$(extract_var CLAUDE_CODE_OAUTH_TOKEN)
if [[ -z "$TOKEN" ]]; then
  echo "scrape_usage.sh: CLAUDE_CODE_OAUTH_TOKEN missing in .env" >&2
  exit 1
fi

CLAUDE_BIN="$(claude_bin)"
[[ -x "$CLAUDE_BIN" ]] || CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "scrape_usage.sh: claude binary not found" >&2
  exit 2
fi

# Build the JS payload as a single tight string (no newlines, no
# trailing quotes that could confuse parsing).
JS=$(cat <<'JSEOF'
(()=>{const bars=document.querySelectorAll('[role="progressbar"]');const meters=[];const PRETTY={'Current session':'5h','All models':'weekly_all','Sonnet only':'weekly_sonnet'};for(const b of bars){const pct=parseInt(b.getAttribute('aria-valuenow'),10);let w=b;let lines=null;for(let i=0;i<8&&w;i++){w=w.parentElement;if(!w)break;const txt=(w.innerText||'').trim();if(txt.length>20&&txt.length<200&&/% used/.test(txt)){lines=txt.split('\n').map(s=>s.trim()).filter(Boolean);break;}}if(!lines)continue;const title=lines[0]||'';if(title.includes('routine'))continue;const resets=(lines.find(l=>l.startsWith('Resets'))||'').replace(/^Resets\s*/,'');meters.push({label:PRETTY[title]||title,pct:pct,resets:resets,raw_label:title})}return JSON.stringify({meters:meters,scraped_at:Math.floor(Date.now()/1000)})})()
JSEOF
)

PROMPT="Run these chromeflow tool calls in order, then reply with ONLY the raw JSON string from step 2 (no markdown, no preamble):
1. switch_to_tab to 'claude.ai/settings/usage'
2. execute_script with code: $JS"

export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
# Haiku is enough for this mechanical task (see the header).
RESPONSE=$(CLAUDE_HOOKS_SUPPRESS_TELEGRAM=1 "$CLAUDE_BIN" \
  --dangerously-skip-permissions \
  --model claude-haiku-4-5 \
  -p "$PROMPT" 2>/dev/null)

# Extract the JSON object from claude's response (may have markdown wrap).
JSON=$(echo "$RESPONSE" | python3 -c "
import sys, re, json
raw = sys.stdin.read()
# Strip markdown code fences if present
m = re.search(r'\{.*\}', raw, re.DOTALL)
if not m:
    print('{\"error\": \"no json in response\", \"raw\": ' + json.dumps(raw[:500]) + '}')
    sys.exit(0)
try:
    parsed = json.loads(m.group(0))
    parsed['fetched_at'] = int(__import__('time').time())
    print(json.dumps(parsed, indent=2))
except Exception as e:
    print(json.dumps({'error': str(e), 'raw': m.group(0)[:500]}))
" 2>/dev/null)

mkdir -p "$REPO_ROOT/state"
echo "$JSON" > "$CACHE"
echo "$JSON"
