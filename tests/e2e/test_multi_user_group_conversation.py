#!/usr/bin/env python3
"""
E2E: two different real users sharing one group room conversation. Groups
are multi-user by design (CLAUDE.md: "In a group several people share one
conversation, so prefix the sender's name... so the assistant knows who is
speaking").
This sends messages from two DISTINCT people in the same persistent claude
session and asserts, via {"echo": true} turns, that each message actually
reaching claude carries the CORRECT sender's name, in the right order, not
just "a name" or the wrong one, and that every message, including a DM
from either of those same people (a different room), always carries the
standing "Telegram" tag in the same bracket (a DM's bracket just has no
name field, since there's only ever one person).

Run: python3 tests/e2e/test_multi_user_group_conversation.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, OWNER_USER_ID  # noqa: E402

GROUP_CHAT_ID = -4900000005
FRIEND_ID = 7000000002


def main():
    h = E2EHarness(label="multi-user-group")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [{"echo": True}] * 4)
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.seed_approved_user(GROUP_CHAT_ID, FRIEND_ID, username="sam")
        h.start_listener()

        h.inject_group_message(GROUP_CHAT_ID, "what's on the agenda today",
                                user_id=int(OWNER_USER_ID), first_name="Alice",
                                message_id=1)
        r1 = h.wait_for_call(
            "editMessageText",
            lambda p: "agenda" in p.get("text", ""), timeout=20)
        text1 = r1["params"].get("text", "")
        print(f"ok: owner's message: {text1!r}")
        assert text1.startswith("[Alice, Telegram]:"), (
            f"expected owner's message prefixed with [Alice, Telegram]:, "
            f"got {text1!r}")

        h.inject_group_message(GROUP_CHAT_ID, "I vote we ship it",
                                user_id=FRIEND_ID, first_name="Sam",
                                message_id=2)
        r2 = h.wait_for_call(
            "editMessageText",
            lambda p: "ship it" in p.get("text", ""), timeout=20)
        text2 = r2["params"].get("text", "")
        print(f"ok: friend's message: {text2!r}")
        assert text2.startswith("[Sam, Telegram]:"), (
            f"expected friend's message prefixed with [Sam, Telegram]:, "
            f"got {text2!r}")
        assert "Alice" not in text2, (
            f"Sam's message must not carry Alice's name, got {text2!r}")

        # Same two people, but a DM (not a group): the name field drops
        # (there's only ever one person), but the "Telegram" tag is on
        # every message regardless of room type - every turn is a standing
        # reminder that this is a real Telegram conversation, not a
        # terminal, which a system-prompt mention alone stopped being
        # salient enough for deep into a long persistent conversation.
        h.inject_dm("quick DM check", user_id=int(OWNER_USER_ID),
                    first_name="Alice", message_id=3)
        r3 = h.wait_for_call(
            "editMessageText",
            lambda p: "quick DM check" in p.get("text", ""), timeout=20)
        text3 = r3["params"].get("text", "")
        print(f"ok: DM message: {text3!r}")
        assert text3.startswith("[Telegram]:"), (
            f"expected DM prefixed with [Telegram]: (no name field), "
            f"got {text3!r}")
        assert "Alice" not in text3, (
            f"a DM's bracket must not carry a name field, got {text3!r}")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)
        for cid in (GROUP_CHAT_ID,):
            state_path = f"{script_path}.state.{cid}"
            if os.path.exists(state_path):
                os.unlink(state_path)

    print("\nok: multi-user group conversation sender-prefixing is correct "
          "and every message, DM included, carries the Telegram tag")


if __name__ == "__main__":
    main()
