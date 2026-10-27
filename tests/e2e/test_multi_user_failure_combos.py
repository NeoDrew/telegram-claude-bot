#!/usr/bin/env python3
"""
E2E: rate-limit and auth-failure handling combined with multiple real
people sharing one group room. Failures are ROOM-scoped, not per-sender:
whichever person's message happens to trigger the 429/401 shouldn't matter,
and a DIFFERENT person's message arriving during the hold must be handled
the same way a same-person follow-up would be (queued + reacted, not
spawned into a doomed retry). Also doubles as a regression combo for the
queue-coalescing character-eating bug (see test_queue_coalesce.sh): a
sender name like "Alice" contains the letter that bug ate.

Two cases:
  1. Person A's message rate-limits the room. Person B (a DIFFERENT real
     person) messages the SAME room while it's still held. Confirm: B gets
     queued + 😴 reacted (not a doomed retry), and once the window clears
     with ZERO further nudges, BOTH people's messages are answered with
     their names intact (not corrupted, not swapped, not dropped).
  2. Person A's message triggers an auth failure. Person B messages the
     same room shortly after. Confirm exactly ONE auth alert fires total
     (not one per sender), and the room doesn't spuriously auto-resume on
     a clock the way a rate limit would (an auth failure isn't time-based).

Run: python3 tests/e2e/test_multi_user_failure_combos.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import (E2EHarness, write_fake_claude_script,  # noqa: E402
                      OWNER_USER_ID)

GROUP_A = -4900000010
GROUP_B = -4900000011
FRIEND_ID = 7000000003
RESETS_IN = 8


def case_multi_user_rate_limit():
    h = E2EHarness(label="multi-user-rate-limit")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"rate_limit": {"resets_in": RESETS_IN, "rate_limit_type": "five_hour"},
         "exit_code": 1},
        {"echo": True},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.seed_approved_user(GROUP_A, FRIEND_ID, username="sam")
        h.start_listener()

        h.inject_group_message(GROUP_A, "Alice's message hits the limit",
                                user_id=int(OWNER_USER_ID), first_name="Alice",
                                message_id=1)
        h.wait_for_call("sendMessage",
                         lambda p: "Rate limited" in p.get("text", ""),
                         timeout=20)
        print("ok: Alice's message parked the room")

        h.inject_group_message(GROUP_A, "Sam's message arrives mid-hold",
                                user_id=FRIEND_ID, first_name="Sam",
                                message_id=2)
        time.sleep(2)
        sleepy = [c for c in h.all_calls("setMessageReaction")
                  if "\U0001F634" in c["params"].get("reaction", "")
                  and str(c["params"].get("message_id")) == "2"]
        assert sleepy, (
            f"expected Sam's mid-hold message to get a 😴 react like "
            f"Alice's would, got "
            f"{[c['params'] for c in h.all_calls('setMessageReaction')]}")
        print("ok: a DIFFERENT person's message mid-hold is treated the "
              "same as a same-person follow-up (😴 react, queued)")

        deadline = time.time() + 40
        resumed = None
        while time.time() < deadline:
            for c in h.all_calls("editMessageText"):
                t = c["params"].get("text", "")
                if "Sam" in t and "Alice" in t:
                    resumed = t
                    break
            if resumed:
                break
            time.sleep(0.5)
        assert resumed, (
            "resumed turn never arrived, or didn't contain both people's "
            "names intact\n" + h._dump_calls())
        print(f"ok: both people's messages answered, names intact after "
              f"coalescing: {resumed!r}")
        assert "Alic\n" not in resumed, (
            "sender names show signs of the queue-coalescing character-"
            "eating bug regressing")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()
        os.unlink(script_path)


def case_multi_user_auth_failure():
    h = E2EHarness(label="multi-user-auth-failure")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "Failed to authenticate. API Error: 401 Invalid "
                  "authentication credentials"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.seed_approved_user(GROUP_B, FRIEND_ID, username="sam")
        h.start_listener()

        h.inject_group_message(GROUP_B, "Alice's message hits an auth error",
                                user_id=int(OWNER_USER_ID), first_name="Alice",
                                message_id=1)
        h.wait_for_call(
            "sendMessage",
            lambda p: "oauth token expired" in p.get("text", "").lower(),
            timeout=20)
        print("ok: Alice's message surfaced the auth alert")

        # An auth failure ends the persistent session (claude keeps
        # running but nothing more happens for THIS turn); a second
        # person's message shortly after must not trigger a SECOND alert
        # for the same underlying problem.
        h.inject_group_message(GROUP_B, "Sam tries too", user_id=FRIEND_ID,
                                first_name="Sam", message_id=2)
        time.sleep(3)

        auth_alerts = [c for c in h.all_calls("sendMessage")
                       if "oauth token expired" in c["params"].get("text", "").lower()]
        assert len(auth_alerts) == 1, (
            f"expected exactly ONE auth alert regardless of how many "
            f"different people message the room, got {len(auth_alerts)}")
        print("ok: exactly one auth alert total, not one per sender")

        rate_limit_notices = [c for c in h.all_calls("sendMessage")
                               if "Rate limited" in c["params"].get("text", "")]
        assert not rate_limit_notices, (
            f"an auth failure must never be treated as a rate limit "
            f"(auto-resume-on-a-clock would be wrong here, a human has to "
            f"fix the token): {rate_limit_notices}")
        print("ok: not treated as a time-based rate limit")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()
        os.unlink(script_path)


def main():
    results = {
        "multi_user_rate_limit": case_multi_user_rate_limit(),
        "multi_user_auth_failure": case_multi_user_auth_failure(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
