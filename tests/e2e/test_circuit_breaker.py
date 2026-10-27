#!/usr/bin/env python3
"""
E2E: the crash-loop circuit breaker (scripts/listener.sh's start_spawn,
".crash_times" check). Trips after >=3 crashes within 60 real seconds,
resets the session and truncates inbox and queue, then retries the message
it was about to send ONCE in the fresh session. BUG FIXED: it used to drop
that message and ask the user to resend. A second trip within 10 minutes
stops for real (the message itself is likely the problem).

  1. recovers: three crashes, then the fresh session answers the message
  2. keeps crashing: the second trip stops retrying and everything goes quiet

This test sends exactly ONE user message and scripts several consecutive
crash turns. A crash's own exit handler salvages the never-answered inbox
message back into queue.jsonl and drain_queue_if_idle auto-retries it, so
a single poisoned prompt naturally compounds into a real crash loop, the
same way a real bug would: no test code manually re-sends the message.

Run: python3 tests/e2e/test_circuit_breaker.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def run_case(label, turns, check):
    h = E2EHarness(label=label)
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, turns)
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()
        h.inject_dm("this message will keep crashing")
        check(h)
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()
        os.unlink(script_path)
        state_path = f"{script_path}.state.{DM_CHAT_ID}"
        if os.path.exists(state_path):
            os.unlink(state_path)


def check_recovers(h):
    t0 = time.time()
    h.wait_for_call("sendMessage",
                    lambda p: "retrying your message" in p.get("text", ""), timeout=90)
    print(f"ok: breaker tripped after {time.time() - t0:.1f}s and retries in a fresh session")
    crashes = sum(1 for c in h.all_calls("sendMessage")
                  if "stopped unexpectedly" in c["params"].get("text", ""))
    assert crashes >= 3, f"breaker needs >=3 crashes, saw {crashes}"
    h.wait_for_call("editMessageText",
                    lambda p: "answered in the fresh session" in p.get("text", ""), timeout=30)
    print("ok: the message was answered in the fresh session, not dropped")


def check_gives_up(h):
    h.wait_for_call("sendMessage",
                    lambda p: "retrying your message" in p.get("text", ""), timeout=90)
    print("ok: first trip retries in a fresh session")
    h.wait_for_call("sendMessage",
                    lambda p: "stopped retrying" in p.get("text", ""), timeout=120)
    print("ok: second trip within 10 minutes stops for real")
    inbox = h.read_room_file(DM_CHAT_ID, "inbox.ndjson", default="")
    queue = h.read_room_file(DM_CHAT_ID, "queue.jsonl", default="")
    assert inbox.strip() == "" and queue.strip() == "", (inbox, queue)
    before = len(h.all_calls("sendMessage"))
    time.sleep(6)
    assert len(h.all_calls("sendMessage")) == before, "the room kept spawning after giving up"
    print("ok: inbox and queue empty, no further activity")


def main():
    crash = {"crash": True, "exit_code": 1}
    results = {
        "recovers_in_fresh_session": run_case(
            "circuit-breaker-recovers", [crash] * 3 + [{"text": "answered in the fresh session"}],
            check_recovers),
        "gives_up_on_second_trip": run_case(
            "circuit-breaker-gives-up", [crash] * 10, check_gives_up),
    }
    for name, ok in results.items():
        print(("PASS " if ok else "FAIL ") + name)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
