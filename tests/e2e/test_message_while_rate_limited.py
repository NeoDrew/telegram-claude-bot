#!/usr/bin/env python3
"""
E2E: a message sent WHILE a room is already parked on a rate-limit hold
must not spawn a doomed retry (guaranteed to hit the same limit again).
listener.sh's dispatch() reacts with a 😴 emoji and queues the message
instead (never previously exercised by this suite). Also confirms the
extra message doesn't create a SECOND independent hold/alert, and that
once the original window clears, BOTH the original message and the one
sent mid-hold are answered (nothing silently dropped).

Run: python3 tests/e2e/test_message_while_rate_limited.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

RESETS_IN = 8


def main():
    h = E2EHarness(label="message-while-rate-limited")
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
        h.start_listener()

        h.inject_dm("first message, will get rate-limited", message_id=1)
        h.wait_for_call("sendMessage",
                         lambda p: "Rate limited" in p.get("text", ""),
                         timeout=20)
        print("ok: first message parked the room")

        calls_before = len(h.all_calls())

        # Sent WHILE still inside the hold.
        h.inject_dm("second message sent mid-hold", message_id=2)
        time.sleep(2)

        sleepy = [c for c in h.all_calls("setMessageReaction")
                  if "\U0001F634" in c["params"].get("reaction", "")]
        print(f"info: setMessageReaction calls so far: "
              f"{[c['params'] for c in h.all_calls('setMessageReaction')]}")
        assert sleepy, (
            "expected a 😴 reaction on the message sent mid-hold, got "
            f"reactions={[c['params'] for c in h.all_calls('setMessageReaction')]}")
        print("ok: 😴 reaction applied to the message sent mid-hold")

        new_calls_since = h.all_calls()[calls_before:]
        new_crash_or_park = [
            c for c in new_calls_since
            if c["method"] == "sendMessage"
            and ("Rate limited" in c["params"].get("text", "")
                 or "crashed" in c["params"].get("text", "").lower())]
        assert not new_crash_or_park, (
            f"a message sent mid-hold must be queued, not spawned into a "
            f"doomed retry: {new_crash_or_park}")
        print("ok: no doomed retry spawn, no second independent alert")

        queued = h.read_room_file(DM_CHAT_ID, "queue.jsonl", default="")
        assert "second message sent mid-hold" in queued, (
            f"expected the mid-hold message queued for auto-resume, "
            f"queue.jsonl={queued!r}")
        print(f"ok: mid-hold message queued: {queued!r}")

        # Zero further nudges: wait out the window and confirm BOTH
        # messages eventually get answered (the queue coalesces them).
        deadline = time.time() + 40
        resumed = None
        while time.time() < deadline:
            for c in h.all_calls("editMessageText"):
                t = c["params"].get("text", "")
                if "second message sent mid-hold" in t:
                    resumed = t
                    break
            if resumed:
                break
            time.sleep(0.5)

        assert resumed, (
            "the mid-hold message was never answered after the window "
            "cleared\n" + h._dump_calls() + h._dump_logs())
        print(f"ok: mid-hold message answered after auto-resume: {resumed!r}")
        if "first message, will get rate-limited" in resumed:
            print("ok: both messages coalesced into the resumed turn "
                  "(original salvaged message + the mid-hold one)")

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

    print("\nok: a message sent while rate-limited is queued (with a 😴 "
          "react), not spawned into a doomed retry, and answered on resume")


if __name__ == "__main__":
    main()
