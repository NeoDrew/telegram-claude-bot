#!/usr/bin/env python3
"""
metrics.py: shared metrics event feed for the dashboard.

Two writers produce ONE consistent NDJSON schema at state/metrics.ndjson:
  - scripts/lib.sh's emit_metric() (bash side: listener.sh, spawn_claude.sh)
  - this module's emit() (python side: stream_to_telegram.py)

Every line is {"ts": "<UTC ISO8601>", "event": "<name>", ...fields}. Field
values are always strings on the bash side (shell has no other type) and
whatever json.dumps produces on the python side (numbers stay numbers);
readers normalize with _num() below rather than assuming either shape.

Event vocabulary (informational, nothing enforces this list, but keeping
new event names to this set is what keeps the dashboard's aggregation
code meaningful):
  turn_end            room, duration_ms, is_error, ctx_pct?
  crash               room, exit_code
  rate_limit_hit      room, limit_type, resume_in_s
  rate_limit_resume    room
  tg_429              backoff_level
  restart_auto        (listener self-restart on stale code)
  restart_manual       (♻️ menu button)
  debounce_coalesce   room, message_count
  stall_detected      room

Deliberately NOT a database and NOT rotated by this module, see
dashboard.py's own trim-on-read for how unbounded growth is handled
without a separate cron job.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METRICS_FILE = REPO_ROOT / "state" / "metrics.ndjson"


def emit(event: str, **fields) -> None:
    """Append one metrics event. Never raises: a metrics write failing
    must not affect the bot's actual behaviour (same contract as
    lib.sh's emit_metric)."""
    try:
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": event, **fields}
        with open(METRICS_FILE, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def read_events(max_age_s: float | None = None):
    """Yield every event dict in the file, oldest first. Corrupt lines
    (a write torn by a concurrent process crash, or hand-editing) are
    skipped rather than aborting the whole read: one bad line must not
    blind the dashboard to everything else in the file.

    max_age_s: if set, skip events older than this many seconds (parsed
    from each event's own "ts", not file mtime); the dashboard almost
    never wants ancient history, and skipping old lines here keeps the
    aggregation cheap even once the file has months of data in it."""
    if not METRICS_FILE.exists():
        return
    cutoff = None
    if max_age_s is not None:
        cutoff = time.time() - max_age_s
    with open(METRICS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            if cutoff is not None:
                ts = _parse_ts(ev.get("ts"))
                if ts is not None and ts < cutoff:
                    continue
            yield ev


def _parse_ts(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def num(value, default=0.0) -> float:
    """Normalize a metric field that might be a JSON number (python
    writer) or a string (bash writer, shell has no numeric type)."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
