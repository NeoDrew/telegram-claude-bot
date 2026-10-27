#!/usr/bin/env python3
"""
E2E: a listener restart WHILE a room is parked on a rate-limit hold must
not lose or corrupt that hold. This models a realistic real-world sequence
for the no-auto-resume bug (see test_rate_limit_auto_resume_no_nudge.py):
someone hits a rate limit, then restarts the bot (♻️ menu
button / launchd kickstart) while debugging or just because they happened
to push an unrelated change, and the NEW listener process must still
respect the still-live .rate_limited_until marker (all state lives on disk
under state/rooms/<chat>/, nothing in the listener's own memory) rather
than either (a) immediately retrying into a guaranteed second failure, or
(b) forgetting about the hold and silently dropping the pending "continue".

Sequence:
  1. DM the bot. It gets rate-limited and parks (session preserved, queued
     "continue").
  2. Restart the listener process (same sandbox, same state/ on disk) WHILE
     still inside the rate-limit window.
  3. Confirm the fresh listener process does NOT immediately retry (no new
     crash/rate-limit alert right after boot).
  4. Wait out the window with zero further nudges. Confirm it still
     auto-resumes correctly, exactly as if the restart never happened.

Run: python3 tests/e2e/test_rate_limit_survives_listener_restart.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

RESETS_IN = 10


def main():
    h = E2EHarness(label="rate-limit-restart-survives")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"rate_limit": {"resets_in": RESETS_IN, "rate_limit_type": "five_hour"},
         "exit_code": 1},
        {"text": "resumed correctly after the listener restart"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("please do a long task")
        h.wait_for_call("sendMessage",
                         lambda p: "Rate limited" in p.get("text", ""),
                         timeout=20)

        marker_path = os.path.join(h.room_dir(DM_CHAT_ID), ".rate_limited_until")
        with open(marker_path) as f:
            resume_at_before = int(f.read().strip())
        remaining = resume_at_before - time.time()
        print(f"ok: parked, {remaining:.1f}s left on the hold, restarting "
              f"the listener now (still inside the window)")
        assert remaining > 1, (
            "test timing too tight: restart needs to happen meaningfully "
            "before the window clears, increase RESETS_IN")

        calls_before_restart = len(h.all_calls())
        h.restart_listener()
        print("ok: listener process restarted, sandbox state untouched")

        # Give the fresh process a moment to run its boot sequence and
        # possibly a premature drain pass, then confirm it did NOT
        # immediately retry into a second failure while still inside the
        # window.
        time.sleep(2)
        marker_after_restart = None
        if os.path.exists(marker_path):
            with open(marker_path) as f:
                marker_after_restart = f.read().strip()
        assert marker_after_restart == str(resume_at_before), (
            f".rate_limited_until changed or vanished across the restart: "
            f"before={resume_at_before} after={marker_after_restart!r} "
            f"(the fresh listener process may have misread/reset the hold)")
        print(f"ok: .rate_limited_until survived the restart unchanged "
              f"({marker_after_restart})")

        new_crash_alerts = [
            c for c in h.all_calls("sendMessage")[calls_before_restart:]
            if "crashed" in c["params"].get("text", "").lower()
            or "Rate limited" in c["params"].get("text", "")]
        assert not new_crash_alerts, (
            f"the fresh listener process retried into a NEW failure while "
            f"still inside the rate-limit window instead of respecting the "
            f"existing hold: {new_crash_alerts}")
        print("ok: no premature retry right after the restart")

        # Zero further nudges from here, same as the base no-nudge test.
        deadline = time.time() + 40
        resumed_text = None
        while time.time() < deadline:
            for c in h.all_calls("editMessageText"):
                t = c["params"].get("text", "")
                if "resumed correctly after the listener restart" in t:
                    resumed_text = t
                    break
            if resumed_text:
                break
            time.sleep(0.5)

        assert resumed_text, (
            "room never auto-resumed after the window cleared, post-"
            "restart, with zero further nudges\n"
            + h._dump_calls() + h._dump_logs())
        print(f"ok: auto-resumed correctly after the restart, with zero "
              f"nudges: {resumed_text!r}")

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

    print("\nok: a listener restart mid-rate-limit-hold does not lose or "
          "corrupt the auto-resume")


if __name__ == "__main__":
    main()
