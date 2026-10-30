#!/usr/bin/env python3
"""render_rooms.py: the unified rooms dashboard, shared by /rooms and the
forget flow so they render identically.

Prints:  <dashboard text>  \x1f  <inline-keyboard JSON>

The text is the rich per-room status (live Telegram title, turn state, cwd,
effort/model, last activity). The keyboard is one 🗑 button per room OTHER
than the current one, firing `/forget ask <id>` (the confirm step). The
caller pipes both halves into reply_kb, which appends the ⬅ Menu row, so
there is no separate cancel button to maintain.

Env: REPO_ROOT, CURRENT_ROOM, TELEGRAM_BOT_TOKEN.
"""
import json
import os
import sys
import time
import subprocess
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "scripts"))
import room_state  # single source of truth for sessions.json
import cache_info

BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
ROOMS = Path(os.environ["REPO_ROOT"]) / "state" / "rooms"
CURRENT = os.environ.get("CURRENT_ROOM", "")


def live_title(cid):
    """Live group title via getChat, or None (DM, failure, no token)."""
    if not BOT:
        return None
    try:
        url = f"{API_BASE}/bot{BOT}/getChat?chat_id={cid}"
        with urllib.request.urlopen(url, timeout=3) as r:
            d = json.load(r)
        if not d.get("ok"):
            return None
        res = d.get("result") or {}
        if res.get("type") in ("group", "supergroup", "channel"):
            return res.get("title") or None
        return None  # DM: getChat returns the owner's own name; don't override
    except Exception:
        return None


def room_name(room, cid):
    """A /label pin wins; otherwise the live Telegram title; else cache/id."""
    cached = (room / "label").read_text().strip() if (room / "label").exists() else ""
    if (room / ".label_pinned").exists():
        return cached or cid
    return live_title(cid) or cached or cid


def state_blurb(st):
    s = st.get("state", "?")
    if s == "streaming":
        return "💬 streaming"
    if s == "tool":
        tn = st.get("tool_name", "?")
        dur = st.get("duration_s")
        return f"🔧 {tn}{f' {dur}s' if dur is not None else ''}"
    if s == "awaiting_result":
        return "⏳ awaiting result"
    if s == "compacting":
        return "🗜 compacting"
    if s == "idle":
        return "💤 idle"
    if s == "no_session":
        return "⚪ no session"
    return "⚫ no spawn"


def collect():
    rooms = []
    if not ROOMS.exists():
        return rooms
    now = time.time()
    for room in sorted(ROOMS.iterdir()):
        if not room.is_dir():
            continue
        cid = room.name
        # Real Telegram chat_ids are numeric (negative for groups), never "0".
        if cid == "0" or not cid.lstrip("-").isdigit():
            continue
        busy_pid = ""
        lock = room / "busy.lock"
        if lock.exists():
            try:
                p = lock.read_text().strip()
                if p:
                    os.kill(int(p), 0)
                    busy_pid = p
            except Exception:
                pass
        sf = str(room / "sessions.json")
        inbox = room / "inbox.ndjson"
        if inbox.exists():
            age = int(now - inbox.stat().st_mtime)
            if age < 60:
                ago = f"{age}s ago"
            elif age < 3600:
                ago = f"{age // 60}m ago"
            elif age < 86400:
                ago = f"{age // 3600}h ago"
            else:
                ago = f"{age // 86400}d ago"
        else:
            ago = "never"
        try:
            sj = subprocess.check_output(
                ["python3", os.environ["REPO_ROOT"] + "/scripts/spawn_state.py", str(room)],
                text=True, timeout=3)
            sstate = json.loads(sj)
        except Exception:
            sstate = {"state": "no_spawn"}
        rooms.append({
            "cid": cid,
            "label": room_name(room, cid),
            "busy_pid": busy_pid,
            "cwd": room_state.get_cwd(sf),
            "sid": room_state.get_session(sf),
            "eff": (room / "effort").read_text().strip() if (room / "effort").exists() else "",
            "mdl": (room / "model").read_text().strip() if (room / "model").exists() else "",
            "ago": ago,
            "kind": "👥" if cid.startswith("-") else "👤",
            "sstate": sstate,
            "cache": cache_info.line(room),
        })

    def sort_key(r):
        is_current = 0 if r["cid"] == CURRENT else 1
        st = r["sstate"].get("state", "no_spawn")
        is_active = 0 if st in ("streaming", "tool", "awaiting_result", "compacting") else 1
        return (is_current, is_active, r["label"].lower())

    rooms.sort(key=sort_key)
    return rooms


def main():
    rooms = collect()
    lines = ["**🌐 Rooms**"]
    note = os.environ.get("FORGOT_NOTE", "")
    if note:
        lines.append(f"_{note}_")
    rows = []
    if not rooms:
        lines.append("(no rooms yet)")
    for r in rooms:
        cwd_short = r["cwd"].replace(os.path.expanduser("~"), "~") if r["cwd"] else "?"
        bits = [state_blurb(r["sstate"]), f"`{cwd_short}`"]
        if r["eff"] and r["eff"] != "default":
            bits.append(f"effort {r['eff']}")
        if r["mdl"]:
            bits.append("model " + (r["mdl"].split("-")[1] if "-" in r["mdl"] else r["mdl"]))
        bits.append(f"last msg {r['ago']}")
        here = "  ← you are here" if r["cid"] == CURRENT else ""
        lines.append(f"\n{r['kind']} **{r['label']}**{here}")
        lines.append(f"  {' · '.join(bits)}")
        if r["cache"]:
            lines.append(f"  {r['cache']}")
        lines.append(f"  _id: `{r['cid']}`_")
        # A forget button for every room except the one you're sending from
        # (you can't forget yourself). Executes immediately, no confirm step.
        if r["cid"] != CURRENT:
            rows.append([{"text": f"🗑 {r['kind']} {r['label']}",
                          "callback_data": f"act:forget yes {r['cid']}"}])

    print("\n".join(lines) + "\x1f" + json.dumps(rows))


if __name__ == "__main__":
    main()
