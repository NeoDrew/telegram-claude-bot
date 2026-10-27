#!/usr/bin/env python3
"""
E2E regression test: a room that hit a 5-hour session limit used to
clear the hold correctly a few minutes later, send a "Ready to continue"
style message, and then do nothing: the original request was never
retried. Now the conversation genuinely resumes.

Root cause: spawn_claude.sh's rate-limit
exit handler only queues a "continue" nudge (so the conversation actually
resumes once the hold clears) inside its "NOT already parked" branch. But
stream_to_telegram.py's own _park_room_for_rate_limit() ALWAYS writes
.rate_limited_until first, on the very same is_error rate-limit result,
before claude even finishes exiting, so by the time spawn_claude.sh's own
post-exit handler runs, the idempotency guard sees an already-set marker
and takes the "already parked, skip" branch on essentially every hit,
not just genuine duplicate re-triggers from a fast retry loop. Whenever
salvage found 0 unconsumed inbox lines (the interrupted turn had already
been read off the inbox but never finished), NOTHING ended up in
queue.jsonl. The hold expired, drain_queue_if_idle found nothing to drain,
and the room sat there with a cheerful "Rate limit cleared" and no actual
resume.

Fix: queue "continue" whenever salvage found 0 messages AND queue.jsonl is
still empty, independent of the already-parked/not-already-parked branch
(that branch still gates the user-facing notify pings, so a genuine
duplicate re-trigger still doesn't spam).

This test forces the SALVAGED=0 condition deterministically (rather than
racing the exact live timing) by pre-seeding .turns_done to already cover
the one inbox line the triggering message becomes, so the salvage step's
own skip-logic treats it as already-answered, the same state an
interrupted turn leaves behind. It then confirms a "continue" lands in queue.jsonl
and the room genuinely auto-resumes once the hold clears, not just that
the marker gets cleared.

Run: python3 tests/e2e/test_rate_limit_zero_salvage_still_continues.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

RESETS_IN = 3  # seconds until the fake rate limit clears


def main():
    h = E2EHarness(label="rate-limit-zero-salvage-continues")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        # delay=1.0 buys a window to seed .turns_done AFTER spawn_claude.sh's
        # own startup has already reset it to 0 (it does that unconditionally
        # before claude even runs) but BEFORE this turn's crash actually
        # fires: the only reliable way to land the seed on the right side
        # of that race from outside the spawn.
        {"delay": 2.5,
         "rate_limit": {"resets_in": RESETS_IN, "rate_limit_type": "five_hour"},
         "exit_code": 1},
        {"text": "Back on it, continuing where we left off."},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        t0 = time.time()
        h.inject_dm("Please do a long task")

        # Seed .turns_done = 1 inside the fake turn's delay window, but only
        # AFTER spawn_claude.sh's own startup has actually reset it to "0"
        # (it does that unconditionally, in the background, some
        # non-deterministic time after inject_dm returns); seeding too
        # early just gets clobbered by that reset. Poll for the "0" to
        # actually land first. The triggering message is inbox line #1;
        # salvage skips the first N lines per .turns_done, so with skip=1
        # and exactly 1 line, it finds nothing new to salvage,
        # deterministically reproducing the SALVAGED=0 condition (a turn
        # that already read its inbox line but never got a clean result
        # for it), without depending on the exact timing race that
        # produces it naturally.
        turns_done_path = os.path.join(
            h.sandbox, "state", "rooms", DM_CHAT_ID, ".turns_done")
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(turns_done_path):
                with open(turns_done_path) as f:
                    if f.read().strip() == "0":
                        break
            time.sleep(0.05)
        else:
            raise AssertionError("spawn never reset .turns_done to 0 in time")
        h.seed_room_file(DM_CHAT_ID, ".turns_done", "1")

        h.wait_for_call(
            "sendMessage",
            lambda p: "Rate limited" in p.get("text", ""),
            timeout=20)
        print("ok: in-room rate-limit notice sent")

        marker_path = os.path.join(
            h.sandbox, "state", "rooms", DM_CHAT_ID, ".rate_limited_until")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(marker_path):
            time.sleep(0.1)
        assert os.path.exists(marker_path), ".rate_limited_until was never written"
        print("ok: .rate_limited_until written")

        # The crux of the fix: even though salvage found 0 messages (per
        # the pre-seeded .turns_done), queue.jsonl must still hold a
        # "continue"; otherwise the hold clearing has nothing to drain.
        queue_path = os.path.join(
            h.sandbox, "state", "rooms", DM_CHAT_ID, "queue.jsonl")
        deadline = time.time() + 10
        queued = ""
        while time.time() < deadline:
            if os.path.exists(queue_path):
                with open(queue_path) as f:
                    queued = f.read()
            if queued.strip():
                break
            time.sleep(0.2)
        assert "continue" in queued, (
            f"expected a 'continue' nudge queued despite 0 salvaged "
            f"messages (that's the whole fix), got queue.jsonl={queued!r}")
        print(f"ok: 'continue' queued despite SALVAGED=0 (queue.jsonl={queued!r})")

        # And the real proof: the room must actually auto-resume once the
        # hold clears, not just clear the marker and go quiet.
        with open(marker_path) as f:
            resume_at = int(f.read().strip())
        sleep_for = max(0.0, resume_at - time.time()) + 0.5
        time.sleep(sleep_for)
        h.inject_dm("/menu")  # nudge the listener's next idle-poll iteration

        resumed_call = h.wait_for_call(
            "editMessageText",
            lambda p: "Back on it, continuing" in p.get("text", ""),
            timeout=20)
        print("ok: room genuinely auto-resumed after the hold cleared")
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

    print("\nok: a rate-limit crash that salvages 0 messages still queues a "
          "continue and genuinely auto-resumes, not just clears the marker")


if __name__ == "__main__":
    main()
