#!/usr/bin/env python3
"""
dashboard.py: local metrics dashboard for telegram-claude-bot.

Serves a single self-contained HTML page (dark, Grafana-style panel grid:
stat tiles + small multiples) backed by a JSON snapshot endpoint the page
polls every few seconds. No build step, no external dependencies:
stdlib http.server + vanilla JS/SVG.

Bound to 127.0.0.1 ONLY, by design. This is a single-user dashboard
for a bot that otherwise has zero inbound network listeners; it must
never be reachable from outside the host it runs on. Do not
change the bind host without re-adding auth first.

build_snapshot() is deliberately separable from the HTTP layer; it's
the testable unit (see tests/test_dashboard_snapshot.py), fed a repo
root and returning a plain dict. The server just calls it per request.

Usage:
    python3 scripts/dashboard.py               # foreground, port 8787
    python3 scripts/dashboard.py --port 9000
    BOT_DASHBOARD_PORT=9000 python3 scripts/dashboard.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import metrics  # noqa: E402
import spawn_state  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_config  # noqa: E402

DEFAULT_PORT = int(bot_config.get("BOT_DASHBOARD_PORT", "8787"))

# Selectable time ranges: range key -> (window_s, bucket_s). Bucket width
# scales with window so every range still renders a readable number of
# points (60-300ish), not 30 days of 5-minute buckets crammed into one SVG.
RANGE_OPTIONS = {
    "1h":  (3600, 60),
    "6h":  (6 * 3600, 5 * 60),
    "24h": (24 * 3600, 5 * 60),
    "7d":  (7 * 24 * 3600, 3600),
    "30d": (30 * 24 * 3600, 6 * 3600),
}
DEFAULT_RANGE = "24h"


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return ""


def _read_int(path: Path):
    raw = _read_text(path)
    if raw.isdigit():
        return int(raw)
    return None


def _room_label(room_dir: Path, chat_id: str) -> str:
    label = _read_text(room_dir / "label")
    return label or chat_id


def _room_status(state_info: dict, rate_limited_until) -> str:
    """good | busy | warning | serious | critical | neutral; drives the
    room badge color. "busy" is deliberately NOT a status-palette color
    (working is not bad); the four true status roles are reserved for
    idle=good, rate-limited=warning, and (below) recent-crash=serious."""
    now = time.time()
    if rate_limited_until and rate_limited_until > now:
        return "warning"
    state = state_info.get("state")
    if state in ("tool", "streaming", "awaiting_result", "compacting"):
        return "busy"
    if state == "idle":
        return "good"
    return "neutral"


def _percentile(values: list[float], pct: float):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(round(pct * (len(s) - 1))))
    return s[idx]


def _recent_crash_times(room_dir: Path, within_s: float = 300.0) -> int:
    """Count .crash_times entries within the last `within_s` seconds,
    a cheap proxy for "is this room actively crash-looping right now"
    without re-deriving the listener's own circuit-breaker logic."""
    raw = _read_text(room_dir / ".crash_times")
    if not raw:
        return 0
    now = time.time()
    n = 0
    for line in raw.splitlines():
        line = line.strip()
        if line.isdigit() and now - int(line) < within_s:
            n += 1
    return n


def _bucket_timeseries(turns, tg429s, now, window_s, bucket_s):
    """Fixed-width time buckets covering [now-window_s, now]. Returns a
    list of {t, turns, errors, avg_duration_ms, tg429} oldest-first,
    the shape the frontend's small-multiple charts draw directly."""
    n_buckets = max(1, int(window_s // bucket_s))
    start = now - n_buckets * bucket_s
    buckets = [{"t": start + i * bucket_s, "turns": 0, "errors": 0,
                "duration_sum": 0.0, "duration_n": 0, "tg429": 0}
               for i in range(n_buckets)]

    def bucket_for(ts):
        idx = int((ts - start) // bucket_s)
        if 0 <= idx < n_buckets:
            return buckets[idx]
        return None

    for e in turns:
        ts = metrics._parse_ts(e.get("ts"))
        if ts is None:
            continue
        b = bucket_for(ts)
        if b is None:
            continue
        b["turns"] += 1
        if e.get("is_error") in (True, "True", "true", "1", 1):
            b["errors"] += 1
        dms = e.get("duration_ms")
        if dms is not None:
            b["duration_sum"] += metrics.num(dms)
            b["duration_n"] += 1

    for e in tg429s:
        ts = metrics._parse_ts(e.get("ts"))
        if ts is None:
            continue
        b = bucket_for(ts)
        if b is not None:
            b["tg429"] += 1

    out = []
    for b in buckets:
        avg = (b["duration_sum"] / b["duration_n"]) if b["duration_n"] else None
        out.append({"t": int(b["t"]), "turns": b["turns"], "errors": b["errors"],
                    "avg_duration_ms": avg, "tg429": b["tg429"]})
    return out


def _version_breakdown(rooms: list[dict], turns: list[dict]) -> list[dict]:
    """Group rooms by their last-known code version (from .spawn_version,
    read per-room in build_snapshot) and cross-reference how many turns in
    the window were actually served under each version (the "chats by
    listener version" panel). A room keeps its last version even once idle,
    so this still shows "who's on what" for quiet rooms, not just busy ones.
    """
    rooms_by_version: dict[str, list[dict]] = defaultdict(list)
    for r in rooms:
        rooms_by_version[r["version"]].append(
            {"chat_id": r["chat_id"], "label": r["label"]})

    turns_by_version = Counter(e.get("version") or "unknown" for e in turns)

    versions = set(rooms_by_version) | set(turns_by_version)
    out = [{
        "version": v,
        "room_count": len(rooms_by_version.get(v, [])),
        "rooms": sorted(rooms_by_version.get(v, []), key=lambda r: r["label"]),
        "turns": turns_by_version.get(v, 0),
    } for v in versions]
    out.sort(key=lambda b: (-b["room_count"], -b["turns"]))
    return out


def build_snapshot(repo_root: Path, range_key: str = DEFAULT_RANGE) -> dict:
    now = time.time()
    if range_key not in RANGE_OPTIONS:
        range_key = DEFAULT_RANGE
    window_s, bucket_s = RANGE_OPTIONS[range_key]

    rooms_dir = repo_root / "state" / "rooms"
    rooms = []
    if rooms_dir.exists():
        for rd in sorted(rooms_dir.iterdir()):
            if not rd.is_dir():
                continue
            chat_id = rd.name
            state_info = spawn_state.classify(rd)
            rate_limited_until = _read_int(rd / ".rate_limited_until")
            rooms.append({
                "chat_id": chat_id,
                "label": _room_label(rd, chat_id),
                "state": state_info.get("state"),
                "status": _room_status(state_info, rate_limited_until),
                "tool_name": state_info.get("tool_name"),
                "duration_s": state_info.get("duration_s"),
                "rate_limited_until": rate_limited_until,
                "recent_crashes": _recent_crash_times(rd),
                "version": _read_text(rd / ".spawn_version") or "unknown",
            })

    events = list(metrics.read_events(max_age_s=window_s))
    turns = [e for e in events if e.get("event") == "turn_end"]
    crashes = [e for e in events if e.get("event") == "crash"]
    rate_hits = [e for e in events if e.get("event") == "rate_limit_hit"]
    tg429s = [e for e in events if e.get("event") == "tg_429"]
    dequeues = [e for e in events if e.get("event") == "dequeue"]
    restarts_auto = [e for e in events if e.get("event") == "restart_auto"]
    restarts_manual = [e for e in events if e.get("event") == "restart_manual"]

    durations = [metrics.num(e.get("duration_ms")) for e in turns
                 if e.get("duration_ms") is not None]
    error_turns = [e for e in turns
                   if e.get("is_error") in (True, "True", "true", "1", 1)]
    batch_sizes = [metrics.num(e.get("count")) for e in dequeues
                   if e.get("count") is not None]

    listener_started_at = _read_int(repo_root / "state" / ".listener_started_at")
    current_version = _read_text(repo_root / "state" / ".listener_version") or "unknown"

    return {
        "generated_at": int(now),
        "range": range_key,
        "window_s": window_s,
        "bucket_s": bucket_s,
        "uptime_s": (now - listener_started_at) if listener_started_at else None,
        "current_version": current_version,
        "rooms": rooms,
        "versions": _version_breakdown(rooms, turns),
        "stats": {
            "turns_total": len(turns),
            "turns_error": len(error_turns),
            "avg_duration_ms": (sum(durations) / len(durations)) if durations else None,
            "p95_duration_ms": _percentile(durations, 0.95),
            "crashes": len(crashes),
            "rate_limit_hits": len(rate_hits),
            "rate_limit_resumes": sum(1 for e in events if e.get("event") == "rate_limit_resume"),
            "tg_429s": len(tg429s),
            "restarts_auto": len(restarts_auto),
            "restarts_manual": len(restarts_manual),
            "avg_batch_size": (sum(batch_sizes) / len(batch_sizes)) if batch_sizes else None,
        },
        "timeseries": _bucket_timeseries(turns, tg429s, now, window_s, bucket_s),
    }


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; this runs in a terminal, not a log file

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self._send(200, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
        elif parsed.path == "/api/snapshot":
            try:
                range_key = parse_qs(parsed.query).get("range", [DEFAULT_RANGE])[0]
                snap = build_snapshot(REPO_ROOT, range_key)
                self._send(200, json.dumps(snap).encode(), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
        else:
            self._send(404, b"not found", "text/plain")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>telegram-claude-bot dashboard</title>
<style>
:root {
  --surface-1:      #1a1a19;
  --page:           #0d0d0d;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --grid:           #2c2c2a;
  --baseline:       #383835;
  --border:         rgba(255,255,255,0.10);
  --series-1:       #3987e5;  /* blue: default single-series trend color */
  --status-good:     #0ca30c;
  --status-warning:  #fab219;
  --status-serious:  #ec835a;
  --status-critical: #d03b3b;
  --status-neutral:  #6b6a64;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px;
}
h1 { font-size: 18px; font-weight: 600; margin: 0 0 4px; }
.subtitle { color: var(--text-secondary); font-size: 13px; margin: 0 0 20px; }
.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
.panel {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px;
}
.panel-title {
  font-size: 12px; color: var(--text-muted); text-transform: uppercase;
  letter-spacing: 0.04em; margin: 0 0 8px;
}
.stat-value { font-size: 30px; font-weight: 600; line-height: 1.1; }
.stat-sub { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.charts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
.chart-panel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.chart-panel svg { width: 100%; height: 140px; overflow: visible; }
.rooms-panel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--text-muted); font-weight: 500; font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.04em; padding: 6px 10px; border-bottom: 1px solid var(--grid); }
td { padding: 8px 10px; border-bottom: 1px solid var(--grid); color: var(--text-secondary); }
tr:last-child td { border-bottom: none; }
.badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 500;
}
.badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.badge-good     { color: var(--status-good);     background: color-mix(in srgb, var(--status-good) 15%, transparent); }
.badge-busy     { color: var(--series-1);        background: color-mix(in srgb, var(--series-1) 15%, transparent); }
.badge-warning  { color: var(--status-warning);  background: color-mix(in srgb, var(--status-warning) 15%, transparent); }
.badge-serious  { color: var(--status-serious);  background: color-mix(in srgb, var(--status-serious) 15%, transparent); }
.badge-critical { color: var(--status-critical); background: color-mix(in srgb, var(--status-critical) 15%, transparent); }
.badge-neutral  { color: var(--status-neutral);  background: color-mix(in srgb, var(--status-neutral) 15%, transparent); }
.room-label { color: var(--text-primary); font-weight: 500; }
.chip { color: var(--text-muted); font-size: 12px; }
.updated { color: var(--text-muted); font-size: 12px; margin-top: 4px; }
.empty { color: var(--text-muted); font-size: 13px; padding: 20px 0; text-align: center; }
.controls { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.btn-group { display: inline-flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.btn-group button {
  background: var(--surface-1); color: var(--text-secondary); border: none;
  border-right: 1px solid var(--border); padding: 5px 12px; font-size: 12px;
  cursor: pointer; font-family: inherit;
}
.btn-group button:last-child { border-right: none; }
.btn-group button:hover { color: var(--text-primary); background: #232322; }
.btn-group button.active { color: var(--page); background: var(--series-1); font-weight: 600; }
.controls-label { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
.chart-panel { position: relative; }
.chart-panel svg { cursor: crosshair; }
#tooltip {
  position: fixed; display: none; pointer-events: none; z-index: 10;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 10px; font-size: 12px; color: var(--text-primary);
  box-shadow: 0 4px 12px rgba(0,0,0,0.4); white-space: nowrap;
}
#tooltip .tt-time { color: var(--text-muted); font-size: 11px; margin-bottom: 2px; }
.version-bar-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.version-bar-track { flex: 1; height: 20px; background: var(--grid); border-radius: 4px; overflow: hidden; }
.version-bar-fill { height: 100%; border-radius: 4px; }
.version-bar-label { width: 220px; flex: 0 0 220px; font-size: 12px; color: var(--text-secondary); display: flex; justify-content: space-between; gap: 6px; }
.version-sha { font-family: "SF Mono", Menlo, monospace; color: var(--text-primary); }
.version-outdated { color: var(--status-warning); }
</style>
</head>
<body>
  <h1>telegram-claude-bot</h1>
  <p class="subtitle" id="subtitle">loading…</p>

  <div class="controls">
    <div>
      <span class="controls-label">Range</span><br>
      <div class="btn-group" id="range-picker"></div>
    </div>
    <div>
      <span class="controls-label">Chart style</span><br>
      <div class="btn-group" id="style-picker">
        <button data-style="line" class="active">Line</button>
        <button data-style="bar">Bar</button>
      </div>
    </div>
  </div>

  <div class="grid" id="stat-tiles"></div>

  <div class="charts">
    <div class="chart-panel">
      <div class="panel-title" id="title-turns">Turns / bucket</div>
      <svg id="chart-turns" viewBox="0 0 600 140" preserveAspectRatio="none"></svg>
    </div>
    <div class="chart-panel">
      <div class="panel-title">Avg turn duration (ms)</div>
      <svg id="chart-duration" viewBox="0 0 600 140" preserveAspectRatio="none"></svg>
    </div>
    <div class="chart-panel">
      <div class="panel-title" id="title-429">Telegram 429s / bucket</div>
      <svg id="chart-429" viewBox="0 0 600 140" preserveAspectRatio="none"></svg>
    </div>
  </div>

  <div class="chart-panel" style="margin-bottom: 16px;">
    <div class="panel-title">Chats by listener version <span class="chip" id="current-version-chip"></span></div>
    <div id="version-bars"></div>
  </div>

  <div class="rooms-panel">
    <div class="panel-title">Rooms</div>
    <table id="rooms-table">
      <thead><tr><th>Room</th><th>Status</th><th>Detail</th><th>Version</th></tr></thead>
      <tbody id="rooms-body"></tbody>
    </table>
  </div>

  <div id="tooltip"></div>

<script>
const STATUS_LABEL = {
  good: "Idle", busy: "Busy", warning: "Rate limited",
  serious: "Crash-looping", critical: "Critical", neutral: "No session",
};
const RANGES = [
  ["1h", "1h"], ["6h", "6h"], ["24h", "24h"], ["7d", "7d"], ["30d", "30d"],
];

let currentRange = "24h";
let chartStyle = "line";
let lastSnap = null;

function fmtDuration(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return Math.round(ms) + "ms";
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + "s";
  return (s / 60).toFixed(1) + "m";
}

function fmtUptime(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtBucketTime(ts, bucketS) {
  const d = new Date(ts * 1000);
  if (bucketS >= 3600) {
    return d.toLocaleDateString(undefined, {month: "short", day: "numeric"}) + " " +
           d.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"});
  }
  return d.toLocaleTimeString();
}

function statTile(title, value, sub) {
  return `<div class="panel">
    <div class="panel-title">${title}</div>
    <div class="stat-value">${value}</div>
    ${sub ? `<div class="stat-sub">${sub}</div>` : ""}
  </div>`;
}

// Renders a single-series line chart per the mark spec: 2px round-cap
// line, series color, ~10% opacity area wash under it, hairline hover
// crosshair, value labeled at the line's end only (never every point);
// a single series needs no legend box (the panel title already names it).
// Stashes points/key/fmt/color on the element itself so attachHover()
// (shared by both chart styles) can compute nearest-point without a
// second render pass.
function renderLineChart(svg, points, key, color, fmt) {
  const W = 600, H = 140, PAD = 8;
  svg.innerHTML = "";
  svg._points = points; svg._key = key; svg._fmt = fmt; svg._color = color;
  if (!points.length) {
    svg.innerHTML = `<text x="${W/2}" y="${H/2}" fill="var(--text-muted)" font-size="12" text-anchor="middle">no data yet</text>`;
    return;
  }
  const vals = points.map(p => p[key] ?? 0);
  const maxV = Math.max(1, ...vals);
  const n = points.length;
  const xAt = i => PAD + (i / Math.max(1, n - 1)) * (W - 2 * PAD);
  const yAt = v => H - PAD - (v / maxV) * (H - 2 * PAD);

  const coords = vals.map((v, i) => [xAt(i), yAt(v)]);
  const linePath = coords.map((c, i) => (i === 0 ? "M" : "L") + c[0].toFixed(1) + "," + c[1].toFixed(1)).join(" ");
  const areaPath = linePath + ` L${coords[coords.length - 1][0].toFixed(1)},${H - PAD} L${coords[0][0].toFixed(1)},${H - PAD} Z`;

  let svgContent = "";
  svgContent += `<line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="var(--baseline)" stroke-width="1"/>`;
  svgContent += `<path d="${areaPath}" fill="${color}" opacity="0.10"/>`;
  svgContent += `<path d="${linePath}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
  const last = coords[coords.length - 1];
  svgContent += `<circle cx="${last[0]}" cy="${last[1]}" r="4" fill="${color}" stroke="var(--surface-1)" stroke-width="2"/>`;
  const lastVal = vals[vals.length - 1];
  svgContent += `<text x="${Math.max(30, last[0] - 4)}" y="${Math.max(12, last[1] - 8)}" fill="var(--text-secondary)" font-size="11" text-anchor="end">${fmt ? fmt(lastVal) : lastVal}</text>`;
  svgContent += `<line class="hover-crosshair" x1="0" y1="${PAD}" x2="0" y2="${H - PAD}" stroke="var(--text-muted)" stroke-width="1" opacity="0"/>`;
  svg.innerHTML = svgContent;
}

// Bar-style alternative for the same data shape: same mark spec minus
// the area wash (bars already read as discrete buckets), one rect per
// point, current-bucket highlighted at full opacity, the rest slightly
// dimmer so the most recent value still reads as the "headline" number.
function renderBarChart(svg, points, key, color, fmt) {
  const W = 600, H = 140, PAD = 8;
  svg.innerHTML = "";
  svg._points = points; svg._key = key; svg._fmt = fmt; svg._color = color;
  if (!points.length) {
    svg.innerHTML = `<text x="${W/2}" y="${H/2}" fill="var(--text-muted)" font-size="12" text-anchor="middle">no data yet</text>`;
    return;
  }
  const vals = points.map(p => p[key] ?? 0);
  const maxV = Math.max(1, ...vals);
  const n = points.length;
  const slot = (W - 2 * PAD) / n;
  const barW = Math.max(1, slot * 0.7);
  const xAt = i => PAD + i * slot + (slot - barW) / 2;
  const yAt = v => H - PAD - (v / maxV) * (H - 2 * PAD);

  let svgContent = `<line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="var(--baseline)" stroke-width="1"/>`;
  vals.forEach((v, i) => {
    const y = yAt(v);
    const isLast = i === n - 1;
    svgContent += `<rect x="${xAt(i).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${(H - PAD - y).toFixed(1)}" fill="${color}" opacity="${isLast ? 1 : 0.55}" rx="1.5"/>`;
  });
  const lastVal = vals[vals.length - 1];
  svgContent += `<text x="${W - PAD}" y="14" fill="var(--text-secondary)" font-size="11" text-anchor="end">${fmt ? fmt(lastVal) : lastVal}</text>`;
  svgContent += `<line class="hover-crosshair" x1="0" y1="${PAD}" x2="0" y2="${H - PAD}" stroke="var(--text-muted)" stroke-width="1" opacity="0"/>`;
  svg.innerHTML = svgContent;
}

function renderChart(svg, points, key, color, fmt) {
  return chartStyle === "bar" ? renderBarChart(svg, points, key, color, fmt)
                               : renderLineChart(svg, points, key, color, fmt);
}

// Shared hover: maps mouse x to the nearest bucket (works identically for
// both chart styles since both scale points across the same PAD..W-PAD
// span) and shows a floating tooltip with the bucket's timestamp + value,
// plus a crosshair line already baked into the SVG by the render fns.
const tooltip = document.getElementById("tooltip");
function attachHover(svg) {
  const W = 600, PAD = 8;
  svg.addEventListener("mousemove", (e) => {
    const points = svg._points;
    if (!points || !points.length) return;
    const rect = svg.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const n = points.length;
    const idx = Math.round(frac * (n - 1));
    const p = points[Math.min(n - 1, Math.max(0, idx))];
    const val = p[svg._key] ?? 0;
    const fmtVal = svg._fmt ? svg._fmt(val) : val;
    const bucketS = (lastSnap && lastSnap.bucket_s) || 300;
    tooltip.innerHTML = `<div class="tt-time">${fmtBucketTime(p.t, bucketS)}</div><div>${fmtVal}</div>`;
    tooltip.style.display = "block";
    tooltip.style.left = (e.clientX + 14) + "px";
    tooltip.style.top = (e.clientY + 14) + "px";
    const cross = svg.querySelector(".hover-crosshair");
    if (cross) {
      const xAt = PAD + (idx / Math.max(1, n - 1)) * (W - 2 * PAD);
      cross.setAttribute("x1", xAt); cross.setAttribute("x2", xAt);
      cross.setAttribute("opacity", "0.5");
    }
  });
  svg.addEventListener("mouseleave", () => {
    tooltip.style.display = "none";
    const cross = svg.querySelector(".hover-crosshair");
    if (cross) cross.setAttribute("opacity", "0");
  });
}
["chart-turns", "chart-duration", "chart-429"].forEach(id => attachHover(document.getElementById(id)));

// "Chats by listener version": plain HTML bars (not SVG): each row is a
// version, bar length is room_count so the eye reads "how many chats",
// hover shows the actual chat list. Any
// version other than the currently-loaded listener code is flagged
// outdated (amber), the live signal for "some rooms haven't picked up
// the latest restart yet".
function renderVersionBars(container, versions, currentVersion) {
  document.getElementById("current-version-chip").textContent = `running: ${currentVersion}`;
  if (!versions.length) {
    container.innerHTML = `<div class="empty">no version data yet</div>`;
    return;
  }
  const maxCount = Math.max(1, ...versions.map(v => v.room_count));
  container.innerHTML = versions.map(v => {
    const outdated = v.version !== currentVersion;
    const pct = Math.max(4, (v.room_count / maxCount) * 100);
    const color = outdated ? "var(--status-warning)" : "var(--series-1)";
    const roomList = v.rooms.map(r => r.label).join(", ") || "(no live rooms, seen in history only)";
    return `<div class="version-bar-row" title="${roomList.replace(/"/g, "&quot;")}">
      <div class="version-bar-label">
        <span class="version-sha ${outdated ? "version-outdated" : ""}">${v.version}${outdated ? " ⚠" : ""}</span>
        <span class="chip">${v.room_count} chat(s) · ${v.turns} turn(s)</span>
      </div>
      <div class="version-bar-track"><div class="version-bar-fill" style="width:${pct}%; background:${color};"></div></div>
    </div>`;
  }).join("");
}

function buildRangePicker() {
  const el = document.getElementById("range-picker");
  el.innerHTML = RANGES.map(([key, label]) =>
    `<button data-range="${key}" class="${key === currentRange ? "active" : ""}">${label}</button>`).join("");
  el.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      currentRange = btn.dataset.range;
      el.querySelectorAll("button").forEach(b => b.classList.toggle("active", b === btn));
      refresh();
    });
  });
}

function buildStylePicker() {
  const el = document.getElementById("style-picker");
  el.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      chartStyle = btn.dataset.style;
      el.querySelectorAll("button").forEach(b => b.classList.toggle("active", b === btn));
      if (lastSnap) renderCharts(lastSnap);
    });
  });
}

function renderCharts(snap) {
  const bucketLabel = snap.range === "1h" ? "min" : "bucket";
  document.getElementById("title-turns").textContent = `Turns / ${bucketLabel} (last ${snap.range})`;
  document.getElementById("title-429").textContent = `Telegram 429s / ${bucketLabel} (last ${snap.range})`;
  renderChart(document.getElementById("chart-turns"), snap.timeseries, "turns", "var(--series-1)", v => Math.round(v));
  renderChart(document.getElementById("chart-duration"), snap.timeseries, "avg_duration_ms", "var(--series-1)", fmtDuration);
  renderChart(document.getElementById("chart-429"), snap.timeseries, "tg429", "var(--status-warning)", v => Math.round(v));
}

async function refresh() {
  let snap;
  try {
    const res = await fetch(`/api/snapshot?range=${encodeURIComponent(currentRange)}`);
    snap = await res.json();
  } catch (e) {
    document.getElementById("subtitle").textContent = "failed to reach dashboard backend; is it still running?";
    return;
  }
  lastSnap = snap;

  document.getElementById("subtitle").innerHTML =
    `uptime ${fmtUptime(snap.uptime_s)} · ${snap.rooms.length} room(s) · window: last ${snap.range}` +
    `<div class="updated">updated ${new Date(snap.generated_at * 1000).toLocaleTimeString()}</div>`;

  const s = snap.stats;
  document.getElementById("stat-tiles").innerHTML = [
    statTile(`Turns (${snap.range})`, s.turns_total, s.turns_error ? `${s.turns_error} error(s)` : "0 errors"),
    statTile("Avg duration", fmtDuration(s.avg_duration_ms), s.p95_duration_ms ? `p95 ${fmtDuration(s.p95_duration_ms)}` : ""),
    statTile("Rate limit hits", s.rate_limit_hits, `${s.rate_limit_resumes} resumed`),
    statTile("Restarts (auto / manual)", `${s.restarts_auto} / ${s.restarts_manual}`, s.crashes ? `${s.crashes} crash(es)` : "0 crashes"),
  ].join("");

  renderCharts(snap);
  renderVersionBars(document.getElementById("version-bars"), snap.versions, snap.current_version);

  const rows = snap.rooms.map(r => {
    const status = r.status || "neutral";
    let detail = "—";
    if (status === "busy" && r.tool_name) detail = `${r.tool_name}${r.duration_s != null ? " · " + r.duration_s + "s" : ""}`;
    else if (status === "warning" && r.rate_limited_until) detail = "resets " + new Date(r.rate_limited_until * 1000).toLocaleTimeString();
    else if (r.recent_crashes) detail = `${r.recent_crashes} crash(es) in the last 5min`;
    const outdated = r.version !== snap.current_version;
    return `<tr>
      <td class="room-label">${r.label} <span class="chip">${r.chat_id}</span></td>
      <td><span class="badge badge-${status}">${STATUS_LABEL[status] || status}</span></td>
      <td>${detail}</td>
      <td><span class="version-sha ${outdated ? "version-outdated" : ""}">${r.version}${outdated ? " ⚠" : ""}</span></td>
    </tr>`;
  });
  document.getElementById("rooms-body").innerHTML = rows.length ? rows.join("")
    : `<tr><td colspan="4" class="empty">no rooms yet</td></tr>`;
}

buildRangePicker();
buildStylePicker();
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard: http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
