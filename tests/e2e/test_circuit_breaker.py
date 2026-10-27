#!/usr/bin/env python3
"""
E2E: the crash-loop circuit breaker (scripts/listener.sh's start_spawn,
".crash_times" check). Trips and resets the room after >=3 crashes within
60 real seconds, truncating BOTH inbox and queue ("tripping means stop
retrying" per CLAUDE.md).

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


def main():
    h = E2EHarness(label="circuit-breaker")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    # Enough consecutive crash turns for the auto-retry loop to reach 3
    # crashes within 60s even if a couple of retries land in the same
    # short-poll cycle. If the breaker logic is broken this fixture would
    # otherwise just crash forever with no natural end state to assert on.
    write_fake_claude_script(script_path, [{"crash": True, "exit_code": 1}] * 8)
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        t0 = time.time()
        h.inject_dm("this message will keep crashing")

        breaker_msg = h.wait_for_call(
            "sendMessage",
            lambda p: "repeated crashes" in p.get("text", ""),
            timeout=90)
        elapsed = time.time() - t0
        print(f"ok: circuit breaker tripped after {elapsed:.1f}s")
        print(f"  text={breaker_msg['params'].get('text', '')!r}")

        assert elapsed < 60, (
            f"breaker tripped at {elapsed:.1f}s but the crash-count window "
            f"is 60s wide, if this consistently lands over ~60s the retry "
            f"cadence may be too slow to ever trip the breaker in practice")

        crash_alerts = h.all_calls("sendMessage")
        real_crash_count = sum(
            1 for c in crash_alerts
            if "crashed (exit" in c["params"].get("text", ""))
        print(f"info: {real_crash_count} individual crash alerts fired "
              f"before the breaker tripped")
        assert real_crash_count >= 3, (
            f"breaker is supposed to require >=3 crashes, but only "
            f"{real_crash_count} individual crash alerts were observed "
            f"(it may be tripping on fewer than the documented threshold)")

        # Tripping means "stop retrying": both files must be empty after.
        inbox = h.read_room_file(DM_CHAT_ID, "inbox.ndjson", default="")
        queue = h.read_room_file(DM_CHAT_ID, "queue.jsonl", default="")
        assert inbox.strip() == "", f"expected inbox truncated, got {inbox!r}"
        assert queue.strip() == "", f"expected queue truncated, got {queue!r}"
        print("ok: inbox.ndjson and queue.jsonl both truncated after trip")

        sid = h.read_room_json(DM_CHAT_ID, "sessions.json", default={}).get("session_id")
        assert not sid, f"expected session_id cleared after breaker trip, got {sid!r}"
        print("ok: session_id cleared after breaker trip")

        # Confirm the loop actually STOPPED: no new crash alert should show
        # up if we wait past another retry cadence.
        before_count = len(h.all_calls("sendMessage"))
        time.sleep(6)
        after_count = len(h.all_calls("sendMessage"))
        assert after_count == before_count, (
            f"breaker tripped but the room kept spawning anyway: "
            f"{before_count} -> {after_count} sendMessage calls in the "
            f"6s after trip")
        print("ok: no further spawn activity after the breaker tripped")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)
        state_path = f"{script_path}.state.{DM_CHAT_ID}"
        if os.path.exists(state_path):
            os.unlink(state_path)

    print("\nok: circuit breaker trips on a real compounding crash loop "
          "and actually stops it")


if __name__ == "__main__":
    main()
