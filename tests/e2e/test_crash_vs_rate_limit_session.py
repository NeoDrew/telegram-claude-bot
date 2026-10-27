#!/usr/bin/env python3
"""
E2E: spawn_claude.sh treats a rate limit and a genuine crash the SAME way as
far as session continuity goes: neither wipes context on its own. A single
crash used to reset session_id unconditionally (any transient blip threw
away the whole conversation even though the crash path was about to retry
the same message with --resume anyway); that was fixed so only a genuine
crash LOOP costs the context, via the listener's circuit breaker (see
test_circuit_breaker.py, which asserts session_id IS cleared once 3+ crashes
land within 60s). This test covers the two single-exit paths directly:

  1. Rate limit: session_id must be IDENTICAL before and after the parked
     exit (same conversation resumes).
  2. A single, non-looping crash: session_id must ALSO be preserved (a
     fresh session_id is NOT minted on the next message); only a real
     crash loop resets it, and that's a separate scenario, not this one.
  3. Crash also stamps .crash_times (so a future loop CAN trip the breaker)
     and sends a "crashed" alert, distinct from the rate-limit "Rate
     limited" in-room notice.

Run: python3 tests/e2e/test_crash_vs_rate_limit_session.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def case_rate_limit_preserves_session():
    h = E2EHarness(label="crash-vs-rl-preserves")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"rate_limit": {"resets_in": 2}, "exit_code": 1},
        {"text": "resumed fine"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("please do work")
        h.wait_for_call("sendMessage",
                         lambda p: "Rate limited" in p.get("text", ""),
                         timeout=20)
        sid_after_park = h.wait_for_room_file(
            DM_CHAT_ID, "sessions.json",
            predicate=lambda s: '"session_id"' in s, timeout=10)
        import json as _json
        sid_before = _json.loads(sid_after_park).get("session_id")
        assert sid_before, "expected a session_id to exist after the first spawn"

        h.wait_for_call("editMessageText",
                         lambda p: "resumed fine" in p.get("text", ""),
                         timeout=20)
        sid_after_resume = h.read_room_json(
            DM_CHAT_ID, "sessions.json", default={}).get("session_id")

        assert sid_after_resume == sid_before, (
            f"rate-limit resume should PRESERVE session_id: "
            f"before={sid_before!r} after={sid_after_resume!r}")
        print(f"ok: session_id preserved across rate-limit park/resume "
              f"({sid_before})")
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    else:
        return True
    finally:
        h.teardown()
        os.unlink(script_path)


def case_single_crash_preserves_session():
    h = E2EHarness(label="crash-vs-rl-preserves-crash")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    # One crash, then a clean turn: the crash path salvages the unanswered
    # message into queue.jsonl and drain_queue_if_idle auto-retries it, so
    # this exercises the SAME real retry-with-full-context flow the rate-
    # limit case does, just via the crash exit path instead.
    write_fake_claude_script(script_path, [
        {"crash": True, "exit_code": 1},
        {"text": "recovered fine"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("please do work")

        crash_alert = h.wait_for_call(
            "sendMessage", lambda p: "crashed" in p.get("text", "").lower(),
            timeout=20)
        print(f"ok: crash alert sent: {crash_alert['params'].get('text', '')!r}")

        sid_after_crash = h.wait_for_room_file(
            DM_CHAT_ID, "sessions.json",
            predicate=lambda s: '"session_id"' in s, timeout=10)
        import json as _json
        sid_before = _json.loads(sid_after_crash).get("session_id")
        assert sid_before, "expected a session_id to still exist after a single crash"

        crash_times = h.wait_for_room_file(
            DM_CHAT_ID, ".crash_times",
            predicate=lambda s: s.strip(), timeout=10)
        assert crash_times.strip(), "expected .crash_times to have an entry"
        print(f"ok: .crash_times stamped ({crash_times.strip()!r}) "
              f"(so a future LOOP could still trip the breaker)")

        # The salvaged message auto-retries via drain_queue_if_idle. If it
        # resumes with a FRESH session_id, this is a NEW session, not a
        # preserved one; the reply landing at all also confirms the retry
        # actually happened.
        h.wait_for_call("editMessageText",
                         lambda p: "recovered fine" in p.get("text", ""),
                         timeout=20)
        sid_after_retry = h.read_room_json(
            DM_CHAT_ID, "sessions.json", default={}).get("session_id")
        assert sid_after_retry == sid_before, (
            f"a single (non-looping) crash should PRESERVE session_id: "
            f"before={sid_before!r} after={sid_after_retry!r}")
        print(f"ok: session_id preserved across a single crash + retry "
              f"({sid_before})")

        rate_limit_notice = [c for c in h.all_calls("sendMessage")
                              if "Rate limited" in c["params"].get("text", "")]
        assert not rate_limit_notice, (
            f"a genuine crash must not be mistaken for a rate limit, got "
            f"{rate_limit_notice}")
        print("ok: crash was not misclassified as a rate limit")
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    else:
        return True
    finally:
        h.teardown()
        os.unlink(script_path)


def main():
    results = {
        "rate_limit_preserves_session": case_rate_limit_preserves_session(),
        "single_crash_preserves_session": case_single_crash_preserves_session(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
