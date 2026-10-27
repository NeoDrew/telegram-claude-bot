#!/usr/bin/env python3
"""
spawn_state.py: classify what a room's claude spawn is doing RIGHT NOW.

The bot's old `is_busy()` was wrapper-process aware (tail -f alive?) but
not turn-state aware. With persistent mode the tail is always alive, so
every room showed "busy" forever. This module reads the active
transcript and classifies the real state:

    idle:              last assistant has stop_reason, nothing pending
    streaming:         claude is currently emitting text/tool_use
    tool:              claude emitted a tool_use, no matching tool_result yet
    awaiting_result:   tool_result just arrived, claude hasn't responded yet
    compacting:        the streamer saw a context compaction start (.compacting)
    no_session:        spawn alive but no transcript at the expected path
    no_spawn:          no spawn process at all

Output is a dict suitable for /status and /ping. CLI form prints JSON.

Usage as CLI:
    spawn_state.py <room_state_dir>

Returns JSON like:
    {
      "state": "tool",
      "tool_name": "Bash",
      "tool_preview": "sleep 300 && check",
      "duration_s": 47,
      "session_id": "00000000-0000-0000-0000-000000000000",
      "transcript_age_s": 47,
      "wrapper_pid_alive": true
    }
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"


def find_transcript(sid: str) -> Path | None:
    if not sid or not PROJECTS.exists():
        return None
    target = f"{sid}.jsonl"
    for proj in PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        cand = proj / target
        if cand.exists():
            return cand
    return None


def proc_alive(pid: int | str) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _last_relevant_events(tx: Path, n: int = 40) -> list[dict]:
    """Read the last N transcript lines that are assistant/user/system-state."""
    try:
        # Read tail efficiently: seek to a chunk near the end.
        size = tx.stat().st_size
        # ~250 chars/line × 40 lines = 10k, but tool_result can be huge.
        # Cap at 256KB to handle big results.
        chunk = min(size, 262_144)
        with open(tx, "rb") as f:
            f.seek(max(0, size - chunk))
            tail = f.read()
        lines = tail.decode("utf-8", errors="ignore").splitlines()
        # Drop the first (likely truncated) line if we didn't read from BOF.
        if size > chunk:
            lines = lines[1:]
        out = []
        for raw in reversed(lines):
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            if t in ("assistant", "user", "system"):
                out.append(d)
                if len(out) >= n:
                    break
        out.reverse()
        return out
    except Exception:
        return []


def _summarize_tool_use(block: dict) -> tuple[str, str]:
    """Return (tool_name, short_preview)."""
    name = block.get("name", "?")
    inp = block.get("input") or {}
    preview = ""
    if name == "Bash":
        preview = (inp.get("command") or "").replace("\n", " ⏎ ")[:80]
    elif name in ("Read", "Write"):
        fp = inp.get("file_path") or ""
        preview = fp.split("/")[-1][:60]
    elif name == "Edit":
        fp = inp.get("file_path") or ""
        preview = fp.split("/")[-1][:60]
    elif isinstance(inp, dict):
        preview = ", ".join(
            f"{k}={str(v)[:30]}" for k, v in list(inp.items())[:2]
        )[:80]
    return name, preview


def _ts_seconds(d: dict) -> float | None:
    raw = d.get("timestamp")
    if not raw:
        return None
    try:
        # ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SS.sssZ) → epoch
        from datetime import datetime
        s = raw.rstrip("Z")
        # fromisoformat handles +00:00; replace Z
        if "+" not in s and "T" in s:
            s += "+00:00"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def classify(room_state_dir: Path) -> dict:
    """Read room state files + transcript, return state dict."""
    sessions_file = room_state_dir / "sessions.json"
    busy_lock = room_state_dir / "busy.lock"

    result = {
        "state": "no_spawn",
        "tool_name": None,
        "tool_preview": None,
        "duration_s": None,
        "session_id": None,
        "transcript_age_s": None,
        "wrapper_pid_alive": False,
        "wrapper_pid": None,
    }

    # Wrapper liveness (for zombie detection, orthogonal to turn state)
    if busy_lock.exists():
        try:
            pid = busy_lock.read_text().strip()
            if pid and proc_alive(pid):
                result["wrapper_pid_alive"] = True
                result["wrapper_pid"] = int(pid)
        except Exception:
            pass

    # Find the live session id. Prefer the one from a live claude
    # process pointed by busy.lock; fall back to sessions.json.
    sid = ""
    if result["wrapper_pid_alive"]:
        # Walk children of wrapper pid for `claude --session-id <uuid>` or `--resume <uuid>`
        try:
            import subprocess
            out = subprocess.check_output(
                ["pgrep", "-P", str(result["wrapper_pid"])],
                text=True, timeout=2).split()
            for child in out:
                cmd = subprocess.check_output(
                    ["ps", "-p", child, "-o", "command="],
                    text=True, timeout=2)
                for token in ("--session-id", "--resume"):
                    if token in cmd:
                        parts = cmd.split()
                        for i, p in enumerate(parts):
                            if p == token and i + 1 < len(parts):
                                sid = parts[i + 1]
                                break
                if sid:
                    break
        except Exception:
            pass
    if not sid and sessions_file.exists():
        try:
            d = json.loads(sessions_file.read_text())
            sid = d.get("session_id") or ""
        except Exception:
            pass
    result["session_id"] = sid

    if not sid:
        if result["wrapper_pid_alive"]:
            result["state"] = "no_session"
        return result

    # The streamer marks a compaction in progress: Claude reads nothing
    # until it finishes (often minutes), so this is not "idle".
    if result["wrapper_pid_alive"] and (room_state_dir / ".compacting").exists():
        result["state"] = "compacting"
        return result

    tx = find_transcript(sid)
    if not tx:
        result["state"] = "no_session"
        return result

    now = time.time()
    result["transcript_age_s"] = int(now - tx.stat().st_mtime)

    events = _last_relevant_events(tx)
    if not events:
        result["state"] = "idle"
        return result

    # Collect pending tool_use ids (no matching tool_result yet) over the
    # tail. Walk forward.
    pending_tool_uses: dict[str, dict] = {}  # tool_use_id -> {name, preview, ts}
    last_assistant_stop_reason = None
    last_tool_result_ts = None

    for ev in events:
        t = ev.get("type")
        if t == "assistant":
            msg = ev.get("message") or {}
            for b in msg.get("content") or []:
                btype = b.get("type")
                if btype == "tool_use":
                    name, preview = _summarize_tool_use(b)
                    pending_tool_uses[b.get("id", "")] = {
                        "name": name,
                        "preview": preview,
                        "ts": _ts_seconds(ev) or now,
                    }
            # stop_reason at the top of message (not block)
            sr = msg.get("stop_reason")
            if sr is not None:
                last_assistant_stop_reason = sr
        elif t == "user":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        tid = c.get("tool_use_id")
                        if tid and tid in pending_tool_uses:
                            del pending_tool_uses[tid]
                        last_tool_result_ts = _ts_seconds(ev) or now

    # Did the most recent event end with a tool_result (no following
    # assistant)? That means claude has been handed results and owes us
    # the next assistant message, either streaming back or with an API
    # call in flight. The previous check (last_tool_result_ts strictly
    # GREATER than events[-1].ts) could never fire in the common case
    # because events[-1] WAS the tool_result, so its timestamp equalled
    # last_tool_result_ts. That's the bug that made /menu say "idle"
    # mid-tool-call.
    last_ev = events[-1] if events else None
    last_event_is_tool_result = bool(
        last_ev
        and last_ev.get("type") == "user"
        and isinstance((last_ev.get("message") or {}).get("content"), list)
        and any(
            isinstance(c, dict) and c.get("type") == "tool_result"
            for c in (last_ev.get("message") or {}).get("content") or []
        )
    )

    # Classify based on the final state.
    age = result["transcript_age_s"]
    if pending_tool_uses:
        # Pick the OLDEST pending tool_use as the "currently running" one
        # (claude usually emits multiple at once but waits on all together).
        oldest = min(pending_tool_uses.values(), key=lambda x: x["ts"])
        result["state"] = "tool"
        result["tool_name"] = oldest["name"]
        result["tool_preview"] = oldest["preview"]
        result["duration_s"] = int(now - oldest["ts"])
    elif last_event_is_tool_result or last_assistant_stop_reason == "tool_use":
        # Tool results came back; claude needs to emit the next assistant
        # message. Recent mtime → streaming the response right now; older
        # → API call in flight (or wedged).
        if age < 3:
            result["state"] = "streaming"
        else:
            result["state"] = "awaiting_result"
            result["duration_s"] = age
    elif last_assistant_stop_reason in ("end_turn", "stop_sequence", None):
        # If the last assistant message has stop_reason, and no pending
        # tools, we're between turns. Truly idle. But if mtime is very
        # recent, treat as streaming (claude is still emitting the final
        # message).
        if age < 3:
            result["state"] = "streaming"
        else:
            result["state"] = "idle"
    else:
        # Other stop_reasons (max_tokens etc): treat as idle.
        result["state"] = "idle"

    return result


def emoji_for_injected_state(state: str) -> str:
    """Map a classify() state to the reaction a message injected into the
    live inbox deserves. Only "idle" (genuinely between turns, about to
    check stdin at the very next gap) earns 👀, claude only reads stdin
    BETWEEN turns, never mid-tool-call, so a message landing while the
    room is "tool"/"streaming"/"awaiting_result" sits unread for however
    long the CURRENT turn's remaining work takes (sometimes 20+ more tool
    calls), nothing like "seen, answering imminently". Everything else
    (including no_session/no_spawn/unknown, fail toward the more honest,
    less over-promising signal) gets 🫡, the same "written down, will get
    to it" reaction the busy-but-inbox-not-ready queue path already uses.
    """
    return "👀" if state == "idle" else "🫡"


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: spawn_state.py <room_state_dir>\n")
        sys.exit(2)
    d = classify(Path(sys.argv[1]))
    print(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
