#!/usr/bin/env python3
"""cache_info.py - how long a chat's prompt cache has left, and what a miss costs.

Claude Code caches the conversation prefix server side. Each API call
refreshes it; after the TTL (1 hour on these spawns, 5 minutes on some
setups) the next message pays to write the whole context again instead
of reading it at the cache price.

The streamer records the last call of every turn in
state/rooms/<chat>/.cache_state:
    {"at": epoch, "ttl": seconds, "ctx": tokens, "model": "claude-opus-5-5"}

Prices are list prices in USD per million tokens (input, cache read),
from Anthropic's model table, 2026-10. A cache write is 2x input for the
1 hour cache and 1.25x for the 5 minute one. On a subscription nothing is
billed per token; the figure is the list-price equivalent.

Used by stream_to_telegram.py (record; edits the last footer to say the
cache is cold once it expires), render_rooms.py and
commands/status.sh (CLI: cache_info.py line <room_dir>).
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

PRICES = {   # model id prefix -> (input, cache read)
    "claude-fable-5": (10.00, 0.25),
    "claude-mythos-5": (10.00, 0.25),
    "claude-opus-5-5": (4.00, 0.20),
    "claude-opus-5": (5.00, 0.50),
    "claude-opus-4": (5.00, 0.50),
    "claude-sonnet-5": (2.00, 0.20),
    "claude-sonnet-4": (3.00, 0.30),
    "claude-haiku-5": (0.10, 0.01),
    "claude-haiku-4": (1.00, 0.10),
}


def _price(model: str):
    best = None
    for prefix, p in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, p)
    return best[1] if best else None


def from_result(ev: dict) -> dict | None:
    """Cache state from a result event: last API call's context size, TTL, model."""
    usage = ev.get("usage") or {}
    its = usage.get("iterations") or []
    last = its[-1] if its else usage
    ctx = (last.get("input_tokens", 0) + last.get("cache_read_input_tokens", 0)
           + last.get("cache_creation_input_tokens", 0))
    if not ctx:
        return None
    cc = usage.get("cache_creation") or {}
    ttl = 300 if cc.get("ephemeral_5m_input_tokens") and not cc.get("ephemeral_1h_input_tokens") else 3600
    models = list((ev.get("modelUsage") or {}).keys())
    return {"at": int(time.time()), "ttl": ttl, "ctx": ctx,
            "model": models[0] if models else ""}


def load(room_dir) -> dict | None:
    try:
        return json.loads((Path(room_dir) / ".cache_state").read_text())
    except Exception:
        return None


def costs(st: dict):
    """(miss, hit) list-price USD for the next message's context, or None."""
    p = _price(st.get("model", ""))
    if not p:
        return None
    mult = 2.0 if st.get("ttl", 3600) >= 3600 else 1.25
    ctx = st.get("ctx", 0) / 1e6
    return ctx * p[0] * mult, ctx * p[1]


def remaining(st: dict, now: float | None = None) -> int:
    return int(st.get("at", 0) + st.get("ttl", 3600) - (now or time.time()))


def _money(x: float) -> str:
    return f"${x:.2f}" if x >= 0.1 else f"${x:.3f}"


def cold_note(st: dict) -> str:
    """Added to the last footer once the cache has expired."""
    c = costs(st)
    return (f"_Cache cold · next message ≈ {_money(c[0])}_" if c
            else "_Cache cold_")


def line(room_dir, now: float | None = None) -> str:
    """e.g. "Cache expires in 42m · miss ≈ $2.68 (cached $0.067)"."""
    st = load(room_dir)
    if not st:
        return ""
    left = remaining(st, now)
    c = costs(st)
    if left <= 0:
        return "Cache cold" + (f" · next message ≈ {_money(c[0])}" if c else "")
    m = left // 60
    when = f"{m}m" if m else f"{left}s"
    tail = f" · miss ≈ {_money(c[0])} (cached {_money(c[1])})" if c else ""
    return f"Cache expires in {when}{tail}"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "line":
        print(line(sys.argv[2]))
    else:
        sys.stderr.write("usage: cache_info.py line <room_dir>\n")
        sys.exit(2)
