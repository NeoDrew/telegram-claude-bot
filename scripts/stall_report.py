#!/usr/bin/env python3
"""
stall_report.py - explain why a spawn stalled.

Reads a room's `spawn_diag.jsonl` and `claude.stream.jsonl` and prints a
forensic timeline focused on the last (or chosen) stall: when did the
stream go silent, what was the TCP state at that moment, did wifi/DNS
flake, and where exactly did the API response truncate.

Usage:
    scripts/stall_report.py <chat_id>          # e.g. -1001000000001
    scripts/stall_report.py <chat_id> --all    # report every stall, not just the last
    scripts/stall_report.py --room <dir>       # explicit room dir (rare)

Output is plain text suitable for paste into a Telegram message or an
Anthropic bug report. The goal: ONE command answers "what broke and where".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def find_room_dir(chat_id: str) -> Path:
    return REPO / "logs" / "rooms" / chat_id


def load_jsonl(path: Path):
    """Yield (offset_bytes, parsed_dict) per line. Skip lines that don't parse;
    surface the offset of the truncated tail line if any."""
    if not path.exists():
        return
    with open(path, "rb") as f:
        offset = 0
        for raw in f:
            try:
                yield offset, json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                # Truncated tail or corruption. Surface offset so the caller
                # can pinpoint exactly where the stream was cut.
                yield offset, {"_parse_error": True, "_raw_head": raw[:120].decode("utf-8", errors="replace")}
            offset += len(raw)


def find_stalls(diag_events):
    """Pair stall_detected/stall_cleared events from the diag JSONL.
    Returns list of {"start_ts","end_ts","silent_s","forensics","est_conns","duration_s"}."""
    open_stall = None
    stalls = []
    for _, ev in diag_events:
        if ev.get("_parse_error"):
            continue
        kind = ev.get("event")
        if kind == "stall_detected":
            open_stall = {
                "start_ts": ev.get("ts"),
                "silent_s": ev.get("silent_s"),
                "est_conns": ev.get("est_conns"),
                "forensics": ev.get("forensics"),
                "end_ts": None,
                "duration_s": None,
            }
        elif kind == "stall_cleared" and open_stall:
            open_stall["end_ts"] = ev.get("ts")
            open_stall["duration_s"] = ev.get("duration_s")
            stalls.append(open_stall)
            open_stall = None
    if open_stall:
        # Still stalled at end of log.
        stalls.append(open_stall)
    return stalls


def parse_iso(ts):
    if not ts:
        return None
    s = ts.rstrip("Z").split(".")[0]
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def diag_window(diag_events, around_ts, before_s=120, after_s=30):
    """Return the diag snapshots within [around_ts - before_s, around_ts + after_s]."""
    target = parse_iso(around_ts)
    if not target:
        return [ev for _, ev in diag_events if not ev.get("_parse_error")][-20:]
    out = []
    for _, ev in diag_events:
        if ev.get("_parse_error"):
            continue
        t = parse_iso(ev.get("ts"))
        if not t:
            continue
        if target - before_s <= t <= target + after_s:
            out.append(ev)
    return out


def stream_truncation_report(stream_log: Path) -> dict:
    """Walk the stream log; report whether the final line parses cleanly or
    where exactly it truncated. Bot-side smoking gun for an API stream cutoff:
    a truncated tail = the SSE stream was severed mid-frame."""
    if not stream_log.exists():
        return {"present": False}
    size = stream_log.stat().st_size
    total_lines = 0
    last_good_line = None
    truncated = False
    truncated_tail = ""
    for off, ev in load_jsonl(stream_log):
        if ev.get("_parse_error"):
            truncated = True
            truncated_tail = ev.get("_raw_head", "")
        else:
            total_lines += 1
            last_good_line = ev
    final_text = ""
    if last_good_line:
        t = last_good_line.get("type")
        if t == "assistant":
            msg = last_good_line.get("message") or {}
            sr = msg.get("stop_reason")
            usage = msg.get("usage") or {}
            final_text = (f"last full assistant: stop_reason={sr} "
                          f"cache_read={usage.get('cache_read_input_tokens')} "
                          f"output_tokens={usage.get('output_tokens')}")
        elif t == "stream_event":
            inner = last_good_line.get("event") or {}
            final_text = f"last stream_event: {inner.get('type')}"
        else:
            final_text = f"last event type: {t}"
    return {
        "present": True,
        "size": size,
        "lines": total_lines,
        "truncated_tail": truncated,
        "truncated_tail_preview": truncated_tail[:200] if truncated else "",
        "final_event_summary": final_text,
    }


def fmt_proc(p):
    if not p:
        return "proc=null"
    return (f"stat={p.get('stat')} cpu={p.get('pcpu')}% "
            f"rss={p.get('rss_kb',0)//1024}MB etime={p.get('etime')}")


def fmt_tcp(t):
    if not t:
        return "tcp=null"
    a = t.get("anthropic_443") or {}
    if not a:
        return f"tcp_total={t.get('total')} api=NONE"
    parts = ", ".join(f"{k}={v}" for k, v in a.items())
    return f"tcp_total={t.get('total')} api={parts}"


def fmt_wifi(w):
    if not w:
        return "wifi=n/a"
    return (f"wifi={w.get('state')} rssi={w.get('rssi')}dBm "
            f"noise={w.get('noise')} ch={w.get('channel')}")


def fmt_reach_dns(r, d):
    bits = []
    if r:
        bits.append(f"tcp443={'ok' if r.get('ok') else 'FAIL'} {r.get('ms')}ms")
    if d:
        bits.append(f"dns={'ok' if d.get('ok') else 'FAIL'} {d.get('ms')}ms"
                    + (f" → {d['addr']}" if d.get('addr') else ""))
    return " ".join(bits)


def print_stall(stall, diag_events, stream_log):
    """Print a focused human-readable report for one stall."""
    print(f"\n━━━ STALL @ {stall['start_ts']} ━━━")
    if stall["end_ts"]:
        print(f"  cleared @ {stall['end_ts']}  (duration {stall['duration_s']}s)")
    else:
        print(f"  STILL STALLED: never cleared")
    print(f"  initial silent_s={stall['silent_s']} "
          f"established_conns_to_api={stall['est_conns']}")
    if stall.get("forensics"):
        print(f"  forensics dump: {stall['forensics']}")

    window = diag_window(diag_events, stall["start_ts"])
    if window:
        print("\n  ── timeline (snapshots either side of stall) ──")
        for ev in window:
            ts = ev.get("ts", "")[-12:-1]  # HH:MM:SS.mmm
            mark = ""
            if ev.get("event") == "stall_detected":
                mark = "  ⚠️ STALL"
            elif ev.get("event") == "stall_cleared":
                mark = "  ✅ cleared"
            print(f"  {ts}  {fmt_proc(ev.get('proc'))}{mark}")
            print(f"           {fmt_tcp(ev.get('tcp'))}")
            print(f"           {fmt_reach_dns(ev.get('reach'), ev.get('dns'))}")
            print(f"           {fmt_wifi(ev.get('wifi'))}")
            stream = ev.get("stream") or {}
            if stream:
                print(f"           stream age={stream.get('age_s')}s size={stream.get('size')}")

    rep = stream_truncation_report(stream_log)
    if rep.get("present"):
        print(f"\n  ── stream log ({stream_log.name}) ──")
        print(f"  size={rep['size']} parsed_lines={rep['lines']}")
        print(f"  {rep.get('final_event_summary','')}")
        if rep["truncated_tail"]:
            print(f"  ⚠️  STREAM TRUNCATED MID-LINE")
            print(f"  truncated tail starts: {rep['truncated_tail_preview']!r}")
            print(f"  → this is the smoking gun for an SSE stream cutoff.")
        else:
            print(f"  stream log ends on a complete line; cutoff was not"
                  f" inside an SSE frame (claude may have hung on something else).")

    print(f"\n  ── interpretation ──")
    if rep.get("present") and rep["truncated_tail"]:
        # Look up the last tcp snapshot for the connection state.
        tcp_states = []
        for ev in window:
            t = ev.get("tcp") or {}
            a = t.get("anthropic_443") or {}
            tcp_states.append(a)
        last = tcp_states[-1] if tcp_states else {}
        if last.get("ESTABLISHED"):
            print("  TCP socket to api.anthropic.com was still ESTABLISHED when stream went silent.")
            print("  → half-open connection: no FIN, no RST, just no more bytes.")
            print("  → root cause is upstream (Anthropic LB rotation / server reset)")
            print("    OR a network blip mid-stream that claude's HTTP client never noticed.")
            print("  → claude binary lacks application-layer keepalive on long SSE responses.")
        elif last.get("CLOSE_WAIT"):
            print("  TCP socket entered CLOSE_WAIT: remote sent FIN, claude has not read it.")
            print("  → server closed the connection cleanly; claude is wedged on something else,")
            print("    e.g. an internal buffer / event-loop bug.")
        else:
            print("  No live ESTABLISHED/CLOSE_WAIT to api.anthropic.com near the stall.")
            print("  → either the connection died and was removed, or claude never had one open.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("chat_id", nargs="?", help="Telegram chat id (e.g. -1001000000001)")
    ap.add_argument("--room", help="Explicit room dir (overrides chat_id lookup)")
    ap.add_argument("--all", action="store_true",
                    help="Report every stall, not just the last")
    args = ap.parse_args()

    if args.room:
        room = Path(args.room)
    elif args.chat_id:
        room = find_room_dir(args.chat_id)
    else:
        # Default: most-recently-modified room dir.
        rooms_root = REPO / "logs" / "rooms"
        if not rooms_root.exists():
            print("no logs/rooms/ found")
            return 2
        rooms = [d for d in rooms_root.iterdir() if d.is_dir()
                 and (d / "spawn_diag.jsonl").exists()]
        if not rooms:
            print("no room with spawn_diag.jsonl found")
            return 2
        room = max(rooms, key=lambda d: (d / "spawn_diag.jsonl").stat().st_mtime)
        print(f"(auto-selected room: {room.name})")

    diag_path = room / "spawn_diag.jsonl"
    stream_path = room / "claude.stream.jsonl"
    if not diag_path.exists():
        print(f"no {diag_path}: diagnostics never ran for this room")
        return 2

    diag_events = list(load_jsonl(diag_path))
    stalls = find_stalls(diag_events)

    print(f"=== stall_report for {room} ===")
    print(f"diag snapshots: {sum(1 for _,e in diag_events if not e.get('_parse_error'))}")
    print(f"stalls detected: {len(stalls)}")

    if not stalls:
        # Even without a recorded stall, surface the stream log truncation
        # state and the last few snapshots, useful when claude is wedged
        # right now but we haven't crossed the 60s threshold yet.
        rep = stream_truncation_report(stream_path)
        if rep.get("present"):
            print(f"\nstream log: {stream_path}")
            print(f"  {rep.get('final_event_summary','')}")
            if rep["truncated_tail"]:
                print(f"  ⚠️ truncated tail: {rep['truncated_tail_preview']!r}")
        print("\nlast 5 diag snapshots:")
        recent = [e for _, e in diag_events if not e.get("_parse_error")][-5:]
        for ev in recent:
            ts = ev.get("ts", "")[-12:-1]
            print(f"  {ts}  {fmt_proc(ev.get('proc'))}")
            print(f"           {fmt_tcp(ev.get('tcp'))}")
            print(f"           {fmt_reach_dns(ev.get('reach'), ev.get('dns'))}")
        return 0

    to_print = stalls if args.all else stalls[-1:]
    for stall in to_print:
        print_stall(stall, diag_events, stream_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
