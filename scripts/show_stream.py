#!/usr/bin/env python3
"""
show_stream.py - render a room's claude.stream.jsonl as a human-readable
timeline.

The audit log is a flat NDJSON dump of every event claude emits: text
deltas, tool_use envelopes, thinking blocks (with huge base64 signatures),
input_json_delta chunks, message_start/stop, status pings. Useful for
programs, painful for eyes.

This tool flattens it into one line per meaningful event:

    14:06:42  USER     Summarise the open issues…
    14:06:48  TOOL     Read  `myapp/README.md`
    14:06:50  TEXT     Read the README. Opening the issue tracker next.
    14:06:52  TOOL     cf_open_page  `https://example.com/`
    14:06:55  RESULT   (ok)
    14:08:01  STOP     end_turn

Plus optional filters: --tools-only, --text-only, --tail N, --since HH:MM.

Usage:
    show_stream.py <chat_id>                  # most recent N events
    show_stream.py <chat_id> --tail 50        # last 50 events
    show_stream.py <chat_id> --tools-only     # tool calls only
    show_stream.py <chat_id> --text-only      # claude's narration / final answers
    show_stream.py <chat_id> --turns          # one line per turn (compact)
    show_stream.py <chat_id> --raw            # full NDJSON tail (escape hatch)
    show_stream.py                            # auto-pick most active room
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Tools from chromeflow (an optional browser plugin) are shortened to
# "cf_<tool>" and get URL/selector previews; any other MCP tool is shown by
# its bare tool name.
MCP_CHROMEFLOW_PREFIX = "mcp__plugin_chromeflow_chromeflow__"


def short_tool(name: str) -> str:
    if name.startswith(MCP_CHROMEFLOW_PREFIX):
        return "cf_" + name[len(MCP_CHROMEFLOW_PREFIX):]
    if name.startswith("mcp__"):
        return name.rsplit("__", 1)[-1]
    return name


def tool_preview(name: str, inp) -> str:
    if not isinstance(inp, dict) or not inp:
        return ""
    short = short_tool(name)

    def one(s, n=80):
        return str(s or "").replace("\n", " ⏎ ")[:n]

    if name == "Bash":
        return one(inp.get("command") or inp.get("description") or "")
    if name in ("Read", "Edit", "Write", "MultiEdit"):
        fp = inp.get("file_path") or ""
        parts = fp.split("/")
        return one("/".join(parts[-3:]) if len(parts) > 3 else fp)
    if name in ("Grep", "Glob"):
        return one(inp.get("pattern") or "")
    if name in ("WebFetch", "WebSearch"):
        return one(inp.get("url") or inp.get("query") or "")
    if name in ("TaskCreate", "TaskUpdate"):
        return one(inp.get("subject") or inp.get("status")
                   or inp.get("taskId") or "")
    if name in ("Task", "Agent"):
        return one(inp.get("description") or inp.get("subject") or "")
    if name == "ToolSearch":
        return one(inp.get("query") or "")
    if name == "TodoWrite":
        return f"{len(inp.get('todos') or [])} todos"

    if short.startswith("cf_"):
        cf = short[3:]
        if cf == "open_page":
            return one(inp.get("url") or "")
        if cf == "click_element":
            return one(inp.get("textHint") or inp.get("selector") or "")
        if cf == "fill_input":
            return one(f"{inp.get('selector','')} ← {inp.get('value','')}")
        if cf == "type_text":
            return one(inp.get("text") or "")
        if cf == "find_text":
            return one(inp.get("query") or "")
        if cf == "switch_to_tab":
            return one(inp.get("tab") or inp.get("query") or "")
        if cf == "execute_script":
            return one(inp.get("script") or inp.get("code") or "")
        if cf == "get_page_text":
            bits = []
            if inp.get("startIndex"):
                bits.append(f"startIndex={inp['startIndex']}")
            if inp.get("selector"):
                bits.append(f"selector={inp['selector']}")
            return ", ".join(bits) or "(default)"
        if cf == "take_screenshot":
            return one(inp.get("save_to") or inp.get("inline") or "")

    return one(", ".join(f"{k}={str(v)[:30]}"
                          for k, v in list(inp.items())[:2]))


def parse_ts(ev: dict) -> str:
    """Compact HH:MM:SS local-clock from any of the timestamp fields. Falls
    back to '--:--:--' so the column stays aligned even on stream_event
    rows that lack a ts."""
    raw = ev.get("timestamp")
    if not raw:
        # Some stream_event envelopes carry no timestamp; the audit log
        # appends them as they arrive, so we approximate by inheriting the
        # last known ts in main(). For now, blank.
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def find_room_dir(chat_id: str) -> Path:
    return REPO / "logs" / "rooms" / chat_id


def auto_room() -> Path | None:
    root = REPO / "logs" / "rooms"
    if not root.exists():
        return None
    rooms = [d for d in root.iterdir() if d.is_dir()
             and (d / "claude.stream.jsonl").exists()]
    if not rooms:
        return None
    return max(rooms, key=lambda d: (d / "claude.stream.jsonl").stat().st_mtime)


# An "interpreted event" is a tuple (ts, kind, text, raw).
def interpret(ev: dict, last_ts: str, text_buffer: dict) -> list[tuple]:
    """Yield interpreted rows from one NDJSON event.

    text_buffer accumulates text_delta chunks under the current content block
    index so we can print them as one TEXT row at content_block_stop, not 200
    rows of single-character deltas.
    """
    out = []
    t = ev.get("type")
    ts = parse_ts(ev)
    if ts == "--:--:--":
        ts = last_ts

    if t == "user":
        msg = ev.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            out.append((ts, "USER", content[:200], ev))
        elif isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    body = c.get("content", "")
                    if isinstance(body, list):
                        body = "".join(x.get("text", "")
                                       for x in body if isinstance(x, dict))
                    err = c.get("is_error")
                    flag = " [ERROR]" if err else ""
                    out.append((ts, "RESULT", f"{str(body)[:160]}{flag}", ev))
                elif c.get("type") == "text":
                    out.append((ts, "USER", c.get("text", "")[:200], ev))

    elif t == "assistant":
        # The assistant envelope arrives AFTER its constituent stream_events,
        # which we've already rendered as TEXT rows. Re-emitting the same
        # text from the envelope produces visible duplication. Tool_use IS
        # only here (no stream_event equivalent we accumulate), so we keep
        # those. Text blocks are skipped; the stream pass already covered them.
        msg = ev.get("message") or {}
        for c in msg.get("content") or []:
            ct = c.get("type")
            if ct == "tool_use":
                name = c.get("name", "?")
                inp = c.get("input") or {}
                preview = tool_preview(name, inp)
                out.append((ts, "TOOL", f"{short_tool(name)} `{preview}`", ev))
            # text and thinking blocks intentionally dropped here.

        sr = msg.get("stop_reason")
        if sr and sr not in ("tool_use",):
            out.append((ts, "STOP", sr, ev))

    elif t == "stream_event":
        inner = ev.get("event") or {}
        it = inner.get("type")
        delta = inner.get("delta") or {}
        if it == "content_block_delta" and delta.get("type") == "text_delta":
            idx = inner.get("index")
            text_buffer.setdefault(idx, []).append(delta.get("text", ""))
        elif it == "content_block_stop":
            idx = inner.get("index")
            if idx in text_buffer:
                joined = "".join(text_buffer.pop(idx)).strip()
                if joined:
                    out.append((ts, "TEXT",
                                joined.split("\n", 1)[0][:200], ev))
        elif it == "message_stop":
            # Already captured via assistant stop_reason; skip.
            pass

    elif t == "result":
        err = ev.get("is_error")
        msg = ev.get("result") or ""
        kind = "ERROR" if err else "DONE"
        # On a successful result the text repeats the final assistant message
        # which we've already shown via the stream_event TEXT row. Show only
        # the kind on DONE to avoid the third echo; keep the full text on
        # ERROR since that's the only place it appears.
        body = str(msg)[:200] if err else ""
        out.append((ts, kind, body, ev))

    elif t == "rate_limit_event":
        info = ev.get("rate_limit_info") or {}
        out.append((ts, "RATE",
                    f"{info.get('rateLimitType')} util={info.get('utilization')}"
                    f" status={info.get('status')}", ev))

    # system/queue-operation/attachment: skipped (low signal).
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chat_id", nargs="?",
                    help="Telegram chat id (e.g. -1001000000001). Auto-picks "
                         "the most-recently-active room when omitted.")
    ap.add_argument("--room", help="Explicit room dir.")
    ap.add_argument("-n", "--tail", type=int, default=40,
                    help="Show last N interpreted rows (default 40).")
    ap.add_argument("--tools-only", action="store_true",
                    help="Filter to tool calls + results.")
    ap.add_argument("--text-only", action="store_true",
                    help="Filter to claude's narration / final text.")
    ap.add_argument("--turns", action="store_true",
                    help="Compact: one line per turn boundary.")
    ap.add_argument("--raw", action="store_true",
                    help="Print the last N raw NDJSON lines (escape hatch).")
    args = ap.parse_args()

    if args.room:
        room = Path(args.room)
    elif args.chat_id:
        room = find_room_dir(args.chat_id)
    else:
        room = auto_room()
        if not room:
            print("no room with claude.stream.jsonl found", file=sys.stderr)
            return 2
        print(f"(room: {room.name})", file=sys.stderr)

    stream = room / "claude.stream.jsonl"
    if not stream.exists():
        print(f"missing {stream}", file=sys.stderr)
        return 2

    # For --raw we don't interpret at all.
    if args.raw:
        with open(stream) as f:
            tail = f.readlines()[-args.tail:]
        sys.stdout.write("".join(tail))
        return 0

    rows = []
    text_buffer = {}
    last_ts = "--:--:--"
    with open(stream) as f:
        for raw in f:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            new_rows = interpret(ev, last_ts, text_buffer)
            for r in new_rows:
                last_ts = r[0]
            rows.extend(new_rows)

    if args.tools_only:
        rows = [r for r in rows if r[1] in ("TOOL", "RESULT")]
    elif args.text_only:
        rows = [r for r in rows if r[1] in ("USER", "TEXT", "DONE", "ERROR")]
    elif args.turns:
        # Collapse to one line per claude turn. Heuristic: keep TOOL, USER,
        # STOP, DONE, ERROR; drop TEXT (each turn's narration would dominate).
        rows = [r for r in rows
                if r[1] in ("TOOL", "USER", "STOP", "DONE", "ERROR", "RATE")]

    if args.tail and len(rows) > args.tail:
        rows = rows[-args.tail:]

    # Column-aligned output. Kind in fixed-width column so it scans easily.
    for ts, kind, text, _ in rows:
        print(f"  {ts}  {kind:<7} {text}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
