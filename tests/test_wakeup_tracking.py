#!/usr/bin/env python3
"""
test_wakeup_tracking.py - the streamer records the assistant's pending ScheduleWakeup
in state/rooms/<chat>/.wakeup_due so listener.sh can deliver it if Claude
Code never fires it (wakeups are skipped while any background task runs,
which made scheduled wakeups unreliable).

  1. a scheduled wakeup writes .wakeup_due with its due time and prompt,
  2. ScheduleWakeup({stop:true}) removes it,
  3. a turn starting after the due time removes it (the assistant is awake),
  4. a turn starting before the due time leaves it alone.

Run: python3 tests/test_wakeup_tracking.py
"""

import importlib.util
import io
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
CHAT = "-100"
SID = "test-session"
ROOM = os.path.join(REPO, "state", "rooms", CHAT)
DUE = os.path.join(ROOM, ".wakeup_due")


def tool_call(tid, inp):
    yield {"type": "stream_event", "session_id": SID, "event": {"type": "message_start", "message": {}}}
    yield {"type": "stream_event", "session_id": SID, "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": tid, "name": "ScheduleWakeup", "input": {}}}}
    yield {"type": "stream_event", "session_id": SID, "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}}}
    yield {"type": "stream_event", "session_id": SID, "event": {"type": "content_block_stop", "index": 0}}


def result(tid, text):
    yield {"type": "user", "session_id": SID, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def run_streamer(evs):
    class Resp:
        def __init__(self, body):
            self._b = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def fake_urlopen(req, timeout=None):
        return Resp(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())

    urllib.request.urlopen = fake_urlopen
    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in evs) + "\n")
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
    mod.log = lambda m: None
    try:
        mod.main()
    except SystemExit:
        pass


def main():
    os.makedirs(ROOM, exist_ok=True)
    failures = []

    def check(cond, ok, bad):
        print(f"ok: {ok}") if cond else failures.append(bad)

    if os.path.exists(DUE):
        os.remove(DUE)
    run_streamer(list(tool_call("t1", {"delaySeconds": 600, "reason": "r", "prompt": "/loop keep going", "noop": False}))
                 + list(result("t1", "Next wakeup scheduled for 16:18:00 (in 637s). Nothing more to do this turn.")))
    d = json.load(open(DUE)) if os.path.exists(DUE) else {}
    check(abs(d.get("due", 0) - (time.time() + 637)) < 15 and d.get("prompt") == "/loop keep going",
          "scheduled wakeup recorded with due time and prompt",
          f"wakeup not recorded correctly: {d}")

    run_streamer(list(tool_call("t2", {"stop": True}))
                 + list(result("t2", "Loop stopped, cancelled 1 pending wakeup(s).")))
    check(not os.path.exists(DUE), "stop:true clears the pending wakeup", "stop:true left .wakeup_due behind")

    json.dump({"due": int(time.time()) + 600, "prompt": "p"}, open(DUE, "w"))
    run_streamer([{"type": "stream_event", "session_id": SID, "event": {"type": "message_start", "message": {}}}])
    check(os.path.exists(DUE), "a turn before the due time keeps the wakeup", "a turn before the due time cleared the wakeup")

    json.dump({"due": int(time.time()) - 5, "prompt": "p"}, open(DUE, "w"))
    run_streamer([{"type": "stream_event", "session_id": SID, "event": {"type": "message_start", "message": {}}}])
    check(not os.path.exists(DUE), "a turn after the due time clears the wakeup", "a turn after the due time left the wakeup")

    if os.path.exists(DUE):
        os.remove(DUE)
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: wakeup tracking")


if __name__ == "__main__":
    main()
