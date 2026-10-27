#!/usr/bin/env python3
"""
E2E regression test for the stale-`.user_cancel`-marker bug. A room's
`/cd` used to set `.user_cancel` before killing its spawn; that spawn died
via the cleanup() TERM trap, which never deleted the marker, so a much
later, completely unrelated exit for the SAME room read the leftover
marker and misclassified itself as a user cancel, silently skipping
rate-limit/crash detection, the salvage, and the auto-resume queueing that
depends on it.

The fix has two halves, both covered here:

  1. cleanup() (spawn_claude.sh's TERM/INT trap, the ACTUAL exit path for
     a real /cancel, since `exit 130` inside the trap bypasses the normal
     fallthrough exit-classification code entirely) now deletes
     `.user_cancel` itself, so a genuine cancel never leaks the marker.

  2. Defense in depth: even if something else leaks the marker, the normal
     exit-classification code now age-gates it: only a marker <=120s old
     is honoured as a real cancel. A stale one is logged and ignored,
     falling through to real crash/rate-limit detection instead.

Run: python3 tests/e2e/test_stale_user_cancel_marker.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def case_real_cancel_does_not_leak_marker():
    h = E2EHarness(label="stale-cancel-no-leak")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"hang": True},
        {"text": "fresh turn after cancel"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("start a long task")
        h.wait_for_call(
            "sendMessage",
            lambda p: "sleep 999" in p.get("text", ""), timeout=20)

        # act:cancel force -> kill_room_spawn(paint_cancel=1) touches
        # .user_cancel, sends SIGTERM, the wrapper's cleanup() trap fires.
        h.inject_callback_query("act:cancel force", chat_id=DM_CHAT_ID)
        h.wait_for_call(
            "editMessageText",
            lambda p: "cancelled" in p.get("text", "").lower(), timeout=15)
        print("ok: real /cancel completed")

        marker_path = os.path.join(h.room_dir(DM_CHAT_ID), ".user_cancel")
        # cleanup() deletes it as part of teardown; give the process a brief
        # moment to finish exiting rather than asserting the instant the
        # cancel confirmation lands.
        deadline = time.time() + 5
        while time.time() < deadline and os.path.exists(marker_path):
            time.sleep(0.1)
        assert not os.path.exists(marker_path), (
            "cleanup() should delete .user_cancel on a real cancel, but it "
            "is still present; this is exactly the leak that let a stale "
            "marker hijack a later, unrelated exit")
        print("ok: .user_cancel does not leak past a real cancel's cleanup()")
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


def case_stale_marker_does_not_hijack_later_crash():
    h = E2EHarness(label="stale-cancel-no-hijack")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"crash": True, "exit_code": 1},
        {"text": "recovered fine"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        # Plant a leaked marker from a "cancel" that (in this scenario)
        # happened long ago and never got cleaned up: the exact shape of
        # the original bug. Backdate it well past the 120s
        # freshness window the fix checks.
        h.seed_room_file(DM_CHAT_ID, ".user_cancel", "")
        marker_path = os.path.join(h.room_dir(DM_CHAT_ID), ".user_cancel")
        stale_time = time.time() - 3600  # 1 hour old
        os.utime(marker_path, (stale_time, stale_time))
        print("ok: seeded a 1-hour-old .user_cancel marker "
              "(simulating a leaked marker from an unrelated earlier event)")

        h.inject_dm("please do work")

        # If the bug were still present, this exit would be silently
        # swallowed as "(user-cancel) - session preserved" with no crash
        # alert at all, and the retry below would never happen. Requiring
        # the real crash alert is the direct assertion that the stale
        # marker was correctly ignored.
        crash_alert = h.wait_for_call(
            "sendMessage", lambda p: "stopped unexpectedly" in p.get("text", "").lower(),
            timeout=20)
        print(f"ok: crash was correctly detected despite the stale marker: "
              f"{crash_alert['params'].get('text', '')!r}")

        crash_times = h.wait_for_room_file(
            DM_CHAT_ID, ".crash_times",
            predicate=lambda s: s.strip(), timeout=10)
        assert crash_times.strip(), (
            "expected .crash_times stamped - a stale-marker misclassification "
            "would skip this too, since the user-cancel branch never stamps it")
        print(f"ok: .crash_times stamped ({crash_times.strip()!r})")

        assert not os.path.exists(marker_path), (
            "the stale marker should be consumed (deleted) once read, even "
            "though it was ignored as stale")
        print("ok: the stale marker was cleaned up, not left for a THIRD exit")

        # The salvaged message auto-retries; confirms the room wasn't left
        # wedged by the stale-marker path and genuinely recovered.
        h.wait_for_call("editMessageText",
                         lambda p: "recovered fine" in p.get("text", ""),
                         timeout=20)
        print("ok: room auto-retried and recovered normally after the crash")
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
        "real_cancel_does_not_leak_marker": case_real_cancel_does_not_leak_marker(),
        "stale_marker_does_not_hijack_later_crash":
            case_stale_marker_does_not_hijack_later_crash(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
