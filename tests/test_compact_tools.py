#!/usr/bin/env python3
"""
Compact verbosity: tool calls are listed by name in ONE message, edited as
more arrive, and a new list starts after any text Claude writes.

The user's example, replayed through the real streamer main() in a group
chat (the edit path) with verbosity tool_calls=compact, edits=compact:
    Bash, Bash, Bash  ->  "Found a process..."  ->  Bash, Edit  ->  summary
must show exactly:
    "⚙️ Bash, ⚙️ Bash, ⚙️ Bash"   (one message, grown by edits)
    "Found a process..."
    "⚙️ Bash, 📝 Edit"             (a NEW message)
    "Here's the summary"
with no command text anywhere and no diff message. Also checks that a
stored "off" (the old hidden setting) now reads as "compact".

Run: python3 tests/test_compact_tools.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)


def se(ev):
    return {"type": "stream_event", "event": ev}


def tool(i, tid, name, inp):
    return [
        se({"type": "content_block_start", "index": i,
            "content_block": {"type": "tool_use", "id": tid, "name": name}}),
        se({"type": "content_block_delta", "index": i,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}}),
        se({"type": "content_block_stop", "index": i}),
    ]


def text(i, t):
    return [
        se({"type": "content_block_start", "index": i,
            "content_block": {"type": "text", "text": ""}}),
        se({"type": "content_block_delta", "index": i,
            "delta": {"type": "text_delta", "text": t}}),
        se({"type": "content_block_stop", "index": i}),
    ]


def events():
    evs = []
    for n in range(3):   # one assistant message per tool, as the CLI sends
        evs += [se({"type": "message_start"}),
                *tool(0, f"b{n}", "Bash", {"command": f"ps aux #{n}"}),
                se({"type": "message_stop"})]
    evs += [se({"type": "message_start"}),
            *text(0, "Found a process using 16% cpu, investigating"),
            *tool(1, "b3", "Bash", {"command": "top -l 1"}),
            se({"type": "message_stop"}),
            se({"type": "message_start"}),
            *tool(0, "e1", "Edit", {"file_path": "/tmp/x.md",
                                    "old_string": "a", "new_string": "b"}),
            se({"type": "message_stop"}),
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "e1", "name": "Edit",
                 "input": {"file_path": "/tmp/x.md", "old_string": "a",
                           "new_string": "b"}}]}},
            se({"type": "message_start"}),
            *text(0, "Here is the summary of cpu"),
            se({"type": "message_stop"}),
            {"type": "result", "result": "ok", "is_error": False}]
    return evs


def run():
    import urllib.request
    calls = []
    mid = [300]

    class R:
        def __init__(self, b): self.b = b
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.b

    def fake(req, timeout=None):
        m = req.full_url.rsplit("/", 1)[-1]
        p = dict(urllib.parse.parse_qsl(req.data.decode() if req.data else ""))
        calls.append((m, p))
        if m == "sendMessage":
            mid[0] += 1
            return R(json.dumps({"ok": True, "result": {"message_id": mid[0]}}).encode())
        return R(json.dumps({"ok": True, "result": True}).encode())

    urllib.request.urlopen = fake
    chat = "-100200301"
    os.environ.update(TELEGRAM_BOT_TOKEN="t", CHAT_ID=chat, TELEGRAM_CHAT_ID=chat)
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN = "t"
    mod.TELEGRAM_CHAT_ID = chat
    mod.API_URL = "https://api.telegram.org/bott"
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    mod._log_sent = lambda m: None
    mod.REPO = __import__("pathlib").Path(tempfile.mkdtemp())
    v = {"tool_calls": "compact", "edits": "compact", "thinking": "off"}
    mod._load_verbosity = lambda: dict(v)
    mod._verbosity = dict(v)
    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in events()) + "\n")
    mod.main()
    return calls


def final_texts(calls):
    """Message id -> last text shown, in send order."""
    order, last = [], {}
    sent = 300
    for m, p in calls:
        if m == "sendMessage":
            sent += 1
            order.append(sent)
            last[sent] = p.get("text", "")
        elif m == "editMessageText":
            last[int(p["message_id"])] = p.get("text", "")
    return [last[i] for i in order]


def main():
    failures = []
    texts = [t for t in final_texts(run()) if "Worked for" not in t and t != "…"]
    want = ["⚙️ Bash, ⚙️ Bash, ⚙️ Bash",
            "Found a process using 16% cpu, investigating",
            "⚙️ Bash, 📝 Edit",
            "Here is the summary of cpu"]
    if texts != want:
        failures.append(f"chat should read {want}, got {texts}")
    else:
        print("ok: tools grouped in one message per stretch, text in between")
    joined = " ".join(texts)
    if "ps aux" in joined or "top -l" in joined or "/tmp/x.md" in joined:
        failures.append("compact mode leaked tool arguments")
    else:
        print("ok: no commands or file paths shown")

    import verbosity
    d = tempfile.mkdtemp()
    room = os.path.join(d, "state", "rooms", "1")
    os.makedirs(room)
    with open(os.path.join(room, "verbosity"), "w") as f:
        json.dump({"tool_calls": "off", "edits": "off"}, f)
    got = verbosity.load(d, "1")
    if got["tool_calls"] == "compact" and got["edits"] == "compact":
        print("ok: a stored 'off' reads as compact")
    else:
        failures.append(f"old 'off' should read as compact, got {got}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall passed")


if __name__ == "__main__":
    main()
