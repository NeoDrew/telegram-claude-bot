#!/usr/bin/env python3
"""
test_ask_user_expiry.py - an unanswered ask_user question tells the user it
expired and loses its buttons.

BUG FIXED: after the 5 minute timeout Claude carried on, but the question
kept its buttons, looking live, and tapping one did nothing visible. The
server now edits the question to say it expired and sends an empty
keyboard. (Taps on expired or older questions are covered in
tests/test_dispatch.sh.)

Run: python3 tests/test_ask_user_expiry.py
"""
import importlib.util
import json
import os
import sys
import tempfile

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def main():
    room = tempfile.mkdtemp()
    os.environ.update(ROOM_DIR=room, CHAT_ID="1", TELEGRAM_BOT_TOKEN="0:fake")
    spec = importlib.util.spec_from_file_location(
        "ask_user_mcp_server", os.path.join(SCRIPTS, "ask_user_mcp_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    def fake_post(endpoint, **kw):
        calls.append((endpoint, kw))
        return {"result": {"message_id": 77}} if endpoint == "sendMessage" else {}

    mod.tg_post = fake_post
    mod.TIMEOUT = 0.3
    result = mod.handle_ask_user({"prompt": "Ship it?", "options": ["Yes", "No"]})

    failures = []
    if "timed out" not in result:
        failures.append(f"Claude was not told it timed out: {result!r}")
    edits = [kw for e, kw in calls if e == "editMessageText"]
    if not edits:
        failures.append("the question was never edited after the timeout")
    else:
        e = edits[-1]
        if e.get("message_id") != 77 or "No answer within 5 minutes" not in e.get("text", ""):
            failures.append(f"wrong expiry edit: {e}")
        if e.get("reply_markup") != {"inline_keyboard": []}:
            failures.append(f"buttons were not removed: {e.get('reply_markup')}")
    if os.path.exists(os.path.join(room, ".ask_user_pending.json")):
        failures.append("the pending question was left behind")

    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ok: an expired question says so and loses its buttons\n\nall passed")


if __name__ == "__main__":
    main()
