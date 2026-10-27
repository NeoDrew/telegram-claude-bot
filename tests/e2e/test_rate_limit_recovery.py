#!/usr/bin/env python3
"""
E2E: a rate-limit hit auto-resumes once the window clears, running the real
spawn_claude.sh exit/park/salvage logic and the real listener.sh drain loop
against the fakes, exercising exactly the kind of production edge case this
harness exists for.

Scenario:
  1. DM the bot. Fake claude's first turn signals a rate limit and exits
     non-zero (mirrors a real 429 kill), instead of answering.
  2. Assert the real park path fires: an in-room "auto-resuming" notice
     (spawn_claude.sh's notify-cmd call) and a live .rate_limited_until
     marker with a near-future epoch.
  3. Wait past the window. Nudge the listener with a harmless /menu tap
     (its getUpdates poll is a 30s long-poll while idle; without a nudge
     this test would have to wait out that real timeout to see the next
     drain_queue_if_idle pass), which exercises the identical resume code
     path faster without touching production code.
  4. Assert the queued "continue" replays via a fresh --resume spawn and
     the fake claude's second turn (now answering normally) reaches
     Telegram.

Run: python3 tests/e2e/test_rate_limit_recovery.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

RESETS_IN = 3  # seconds until the fake rate limit clears


def main():
    h = E2EHarness(label="rate-limit-recovery")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"rate_limit": {"resets_in": RESETS_IN, "rate_limit_type": "five_hour"},
         "exit_code": 1},
        {"text": "Welcome back, continuing where we left off."},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        t0 = time.time()
        h.inject_dm("Please do a long task")

        # The in-room park notice comes from stream_to_telegram.py's
        # _fmt_error_result() (fired the moment the streamer sees the
        # is_error rate-limit result), not from spawn_claude.sh's own
        # notify-cmd fallback, which fires after and gets skipped as a
        # duplicate once the room is already parked (see logs/spawn.log:
        # "already parked ... skipping duplicate queue+notify").
        park_call = h.wait_for_call(
            "sendMessage",
            lambda p: "Rate limited" in p.get("text", ""),
            timeout=20)
        print("ok: in-room rate-limit notice sent")
        print(f"  text={park_call['params'].get('text', '')!r}")

        marker_path = os.path.join(
            h.sandbox, "state", "rooms", DM_CHAT_ID, ".rate_limited_until")
        with open(marker_path) as f:
            resume_at = int(f.read().strip())
        assert resume_at > time.time(), (
            f".rate_limited_until={resume_at} is not in the future "
            f"(now={time.time()})")
        print(f"ok: .rate_limited_until written, resumes at {resume_at} "
              f"({resume_at - time.time():.1f}s from now)")

        # Informational only, not asserted: spawn_claude.sh's salvage-and-
        # queue-"continue" logic runs in the WRAPPER after the pipeline
        # drains, and the listener's own poll cadence is dynamic (any busy
        # room shortens the NEXT getUpdates timeout to 5s), so with a
        # RESETS_IN this short the room can auto-resume and drain the
        # queue before this check ever gets to see "continue" sitting in
        # it. Either observation (still queued, or already drained because
        # resume already happened) is consistent with correct behaviour;
        # the real proof is the resumed reply landing below.
        queue_path = os.path.join(
            h.sandbox, "state", "rooms", DM_CHAT_ID, "queue.jsonl")
        queued = ""
        if os.path.exists(queue_path):
            with open(queue_path) as f:
                queued = f.read()
        print(f"info: queue.jsonl at this point = {queued!r}")

        # Wait out the window, then nudge so the listener's next loop
        # iteration (which always ends in drain_queue_if_idle) runs promptly
        # instead of sitting out its ~30s idle getUpdates timeout. /menu is
        # deliberately used over another plain-text DM: a second dispatchable
        # text message would spawn its own fake claude process sharing the
        # SAME FAKE_CLAUDE_SCRIPT turn-index state file, stealing the
        # "Welcome back" turn meant for the resumed room. /menu routes to
        # handle_action.sh, never touches spawn_claude.sh or the turn state.
        sleep_for = max(0.0, resume_at - time.time()) + 0.5
        time.sleep(sleep_for)
        h.inject_dm("/menu")

        resumed_call = h.wait_for_call(
            "editMessageText",
            lambda p: "Welcome back, continuing" in p.get("text", ""),
            timeout=20)
        print("ok: auto-resumed turn reached Telegram")
        print(f"  text={resumed_call['params'].get('text', '')!r}")
        print(f"  total wall time: {time.time() - t0:.1f}s")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)
        try:
            os.unlink(script_path + ".state")
        except FileNotFoundError:
            pass

    print("\nok: rate-limit hit auto-resumes end to end against the fakes")


if __name__ == "__main__":
    main()
