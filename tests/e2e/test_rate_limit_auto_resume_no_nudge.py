#!/usr/bin/env python3
"""
E2E: rate-limit auto-resume with ZERO follow-up activity from the user.
Regression test: a rate-limited room used to stay parked until a new
message arrived, instead of auto-resuming on its own. The existing
test_rate_limit_recovery.py nudges the listener with a /menu tap after the
window clears, to force its blocking getUpdates poll to return early rather
than wait out its up-to-30s idle timeout. That nudge means that test never
actually proved the UNASSISTED path works, only proved the mechanism works
once something pokes the listener into looping again.

This test sends the triggering message, then does ABSOLUTELY NOTHING else:
no callback, no second message, no read of internal state to force a
recheck. It just waits, exactly like a user who fires off a message, gets
rate-limited, and closes the app. If the real system needs a nudge that a
real idle user will never provide, this is where that shows up.

Run: python3 tests/e2e/test_rate_limit_auto_resume_no_nudge.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

# Comfortably inside the listener's worst-case idle poll window (30s), so a
# correctly-working auto-resume should land well within one, at most two,
# natural poll cycles with no help at all.
RESETS_IN = 6
# Generous ceiling: one full idle poll cycle (30s) plus resume overhead,
# plus slack. If this needs to be much larger than ~40s to pass reliably,
# that's itself evidence of the no-auto-resume bug, not just a slow but
# working mechanism.
MAX_WAIT = 45


def main():
    h = E2EHarness(label="rate-limit-no-nudge")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"rate_limit": {"resets_in": RESETS_IN, "rate_limit_type": "five_hour"},
         "exit_code": 1},
        {"text": "auto-resumed with zero nudges"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        t0 = time.time()
        h.inject_dm("please do a long task")

        h.wait_for_call("sendMessage",
                         lambda p: "Rate limited" in p.get("text", ""),
                         timeout=20)
        parked_at = time.time()
        print(f"ok: room parked at t={parked_at - t0:.1f}s")

        marker_path = os.path.join(h.room_dir(DM_CHAT_ID), ".rate_limited_until")
        with open(marker_path) as f:
            resume_at = int(f.read().strip())
        print(f"info: resumes at epoch {resume_at} "
              f"({resume_at - time.time():.1f}s from now)")

        # Zero activity from here. No callback, no second message, nothing.
        # Poll ONLY the fake server's call log and the room's own
        # .turns_done counter (never touched by the test itself) so this
        # genuinely measures whether the system heals on its own.
        deadline = t0 + MAX_WAIT
        resumed_text = None
        while time.time() < deadline:
            for c in h.all_calls("editMessageText"):
                t = c["params"].get("text", "")
                if "auto-resumed with zero nudges" in t:
                    resumed_text = t
                    break
            if resumed_text:
                break
            time.sleep(0.5)

        total = time.time() - t0
        assert resumed_text, (
            f"room never auto-resumed within {MAX_WAIT}s of ZERO follow-up "
            f"activity from the user (window was only {RESETS_IN}s): the "
            f"resume needs a nudge an idle user will never provide.\n" + h._dump_calls() + h._dump_logs())
        print(f"ok: auto-resumed with ZERO nudges after {total:.1f}s "
              f"total (window was {RESETS_IN}s): {resumed_text!r}")

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

    print("\nok: rate-limit auto-resume works with zero user activity "
          "after the window clears")


if __name__ == "__main__":
    main()
