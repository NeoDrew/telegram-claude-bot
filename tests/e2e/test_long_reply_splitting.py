#!/usr/bin/env python3
"""
E2E: a reply long enough to blow past Telegram's ~4096-char message cap
(stream_to_telegram.py's MAX_MSG_CHARS=3800 / MAX_HTML_CHARS=4000 safety
margins) must be split across multiple messages, each individually under
the real Telegram limit, with no content silently dropped at the seam.

Run: python3 tests/e2e/test_long_reply_splitting.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

TELEGRAM_HARD_CAP = 4096

# Distinct, greppable markers at the start/middle/end of a long body so a
# split that drops or reorders a chunk is easy to catch, not just "some
# huge string arrived intact or it didn't". Comfortably 2x the ~4000-char
# split threshold (MAX_HTML_CHARS/MAX_MSG_CHARS in stream_to_telegram.py)
# so this reliably exercises the split path rather than landing just under
# it as a single edited message (which is what a smaller fixture did here
# the first time this test was written, a test bug not a product bug: it
# waited on a sendMessage carrying ENDMARKER that was never going to come
# because the whole reply fit in one editMessageText).
LONG_TEXT = (
    "STARTMARKER " + ("filler word " * 350) + " MIDMARKER "
    + ("more filler " * 350) + " ENDMARKER"
)


def main():
    h = E2EHarness(label="long-reply-split")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [{"text": LONG_TEXT}])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("give me a very long answer")
        # Wait on the turn actually completing (bumped by the streamer on
        # every clean result envelope), not on a specific call/method
        # guess for where the tail lands, since that's an implementation
        # detail of the split logic, not the thing under test.
        h.wait_for_room_file(
            DM_CHAT_ID, ".turns_done",
            predicate=lambda s: s.strip() == "1", timeout=45)
        print("ok: turn completed (.turns_done=1)")

        edits = h.all_calls("editMessageText")
        sends = h.all_calls("sendMessage")
        all_bodies = [c["params"].get("text", "") for c in edits + sends]

        for body in all_bodies:
            assert len(body) <= TELEGRAM_HARD_CAP, (
                f"a single Telegram message exceeded the {TELEGRAM_HARD_CAP} "
                f"hard cap: {len(body)} chars")
        print(f"ok: every individual message stayed under "
              f"{TELEGRAM_HARD_CAP} chars ({len(all_bodies)} messages total)")

        joined = "\n".join(all_bodies)
        for marker in ("STARTMARKER", "MIDMARKER", "ENDMARKER"):
            assert marker in joined, (
                f"{marker} missing across all sent/edited bodies, content "
                f"was lost at a split seam")
        print("ok: START/MID/END markers all present somewhere across the "
              "split messages, nothing dropped")

        assert len(all_bodies) >= 2, (
            f"expected the long reply to actually SPLIT into >=2 messages, "
            f"got {len(all_bodies)} (either splitting didn't trigger, or "
            f"everything landed in one call some other way)")
        print(f"ok: reply actually split into {len(all_bodies)} messages "
              f"(confirms the split path triggered, not a no-op)")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)

    print("\nok: long replies split correctly under Telegram's message cap")


if __name__ == "__main__":
    main()
