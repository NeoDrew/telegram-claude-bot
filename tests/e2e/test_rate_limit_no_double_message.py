#!/usr/bin/env python3
"""
E2E regression test: a rate limit hit used to produce TWO separate bot
messages back to back for one single event. Illustrative example of the
two message shapes (example text, matching the fixture below):

  1. Claude Code's own native banner, streamed as ordinary text:
     "You've hit your session limit · resets 10:10pm (UTC)"
  2. This bot's own separately-formatted message:
     "⏸ Rate limited\n  • 5-hour session limit resets in 2h53m (10:10 PM)\n\n
     Messages queued and will retry automatically."

Root cause: stream_to_telegram.py's is_error handler unconditionally sent
its own _fmt_error_result() message whenever the result was rate-limit
flavoured, with no check for whether claude had ALREADY told the user via
ordinary streamed text in the same turn. The fix captures the turn's live
text buffer (_last_live_text) before commit_live() clears it, and skips the
bot's own message when that buffer already contains a rate-limit keyword
(now including "session limit", also added to _RATE_LIMIT_KEYWORDS).

Scenario: fake claude streams the native banner as text, then emits the
usual rate_limit_event + is_error result. Assert exactly ONE bot message
lands in the room for the hit (the native banner, delivered via the normal
commit_live() path), not two.

Run: python3 tests/e2e/test_rate_limit_no_double_message.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

NATIVE_BANNER = "You've hit your session limit · resets 10:10pm (UTC)"


def main():
    h = E2EHarness(label="rate-limit-no-double-message")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"rate_limit": {"resets_in": 300, "rate_limit_type": "five_hour",
                         "native_banner": NATIVE_BANNER},
         "exit_code": 1},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()
        h.inject_dm("Please do a long task")

        # Wait for the native banner to land (delivered via the normal
        # streamed-text path, i.e. an editMessageText finalising commit_live()).
        h.wait_for_call(
            "editMessageText",
            lambda p: "session limit" in p.get("text", "").lower(),
            timeout=20)
        print("ok: claude's native rate-limit banner reached Telegram")

        # Give the (buggy, pre-fix) second message a beat to land if it's
        # going to. spawn_claude.sh's own notify-cmd fallback is already
        # deduped by .rate_limited_until, so the only way a second message
        # appears in-room is the streamer's own redundant _fmt_error_result
        # send; this is exactly what the fix suppresses.
        import time
        time.sleep(3)

        all_calls = h.all_calls()
        room_texts = []
        for c in all_calls:
            if c["method"] not in ("sendMessage", "editMessageText"):
                continue
            p = c["params"]
            if str(p.get("chat_id")) != DM_CHAT_ID:
                continue
            text = p.get("text", "")
            if "session limit" in text.lower() or "rate limited" in text.lower():
                room_texts.append(text)

        distinct = set(room_texts)
        print(f"info: rate-limit-flavoured message bodies seen: {sorted(distinct)!r}")

        assert "Rate limited" not in " ".join(distinct), (
            "the bot's own separately-formatted '⏸ Rate limited' message "
            "should be suppressed once claude's own native banner already "
            f"told the user, but it appeared anyway: {distinct!r}")
        print("ok: the bot's own duplicate '⏸ Rate limited' message did NOT fire")

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

    print("\nok: a rate limit hit with a native claude banner produces exactly "
          "one in-room message, not two")


if __name__ == "__main__":
    main()
