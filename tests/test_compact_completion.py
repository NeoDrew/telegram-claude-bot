#!/usr/bin/env python3
"""
test_compact_completion.py - after a /compact, Claude Code sends the
conversation summary (and "Compacted") as `user` messages whose content is a
plain STRING, not a block list. The streamer's tool-result handler iterated
that string character by character and crashed on str.get(), killing the
room's streamer before the post-result ".compact_in_flight" check could send
the "Context compressed." confirmation, so tapping compact never got a
completion message.

Feeds the real event shape (system compact_boundary, string-content user
messages, then a success result) with .compact_in_flight set, and asserts:
  1. the streamer doesn't crash,
  2. the compact confirmation is fired via notify-cmd,
  3. the marker is consumed.

Run: python3 tests/test_compact_completion.py
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
CHAT = "-100"
SID = "test-session"


def events():
    yield {"type": "system", "subtype": "status", "status": "compacting", "session_id": SID}
    yield {"type": "system", "subtype": "status", "status": None,
           "compact_result": "success", "session_id": SID}
    yield {"type": "system", "subtype": "compact_boundary", "session_id": SID,
           "compact_metadata": {"trigger": "manual", "pre_tokens": 765460,
                                "post_tokens": 13359}}
    yield {"type": "user", "session_id": SID, "message": {
        "role": "user",
        "content": "This session is being continued from a previous conversation."}}
    yield {"type": "user", "session_id": SID, "message": {
        "role": "user",
        "content": "<local-command-stdout>Compacted </local-command-stdout>"}}
    yield {"type": "result", "subtype": "success", "is_error": False,
           "duration_ms": 0, "num_turns": 0, "result": "", "session_id": SID}


def main():
    room = os.path.join(REPO, "state", "rooms", CHAT)
    os.makedirs(room, exist_ok=True)
    marker = os.path.join(room, ".compact_in_flight")
    open(marker, "w").close()

    notified = []
    subprocess.run = lambda args, **kw: notified.append(list(args))

    class Resp:
        def __init__(self, body):
            self._b = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    mid = [100]

    def fake_urlopen(req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        if method == "sendMessage":
            mid[0] += 1
            return Resp(json.dumps({"ok": True, "result": {"message_id": mid[0]}}).encode())
        return Resp(json.dumps({"ok": True, "result": True}).encode())

    import urllib.request
    urllib.request.urlopen = fake_urlopen
    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in events()) + "\n")

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    os.environ["CHAT_ID"] = CHAT
    os.environ["TELEGRAM_CHAT_ID"] = CHAT
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_CHAT_ID = CHAT
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod._log_sent = lambda m: None
    logged = []
    mod.log = lambda m: logged.append(m)

    failures = []
    try:
        mod.main()
    except SystemExit as e:
        if e.code not in (0, None):
            failures.append(f"streamer exited with code {e.code}")

    crashes = [m for m in logged if "crashed" in m]
    if crashes:
        failures.append(f"streamer crashed: {crashes}")
    else:
        print("ok: string-content user messages after /compact don't crash the streamer")

    if not any("Context compressed." in " ".join(a) for a in notified):
        failures.append("the 'Context compressed.' confirmation was never sent")
    else:
        print("ok: compact confirmation fired")

    if os.path.exists(marker):
        os.remove(marker)
        failures.append(".compact_in_flight was never consumed")
    else:
        print("ok: .compact_in_flight consumed")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: /compact completes and confirms")


if __name__ == "__main__":
    main()
