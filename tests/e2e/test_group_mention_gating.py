#!/usr/bin/env python3
"""
E2E: the group "@-mentions only" gate (listener.sh, state/rooms/<chat>/
mentions_only). When on, a group message that doesn't @-mention the bot
must be silently ignored (no spawn); one that does must be dispatched with
the @mention text stripped before it reaches claude.

Uses an {"echo": true} fake claude turn so the test can assert on the
EXACT text that reached claude, not just "something happened", this is
the only way to actually verify the mention-stripping regex rather than
just verifying dispatch occurred.

Run: python3 tests/e2e/test_group_mention_gating.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script  # noqa: E402

GROUP_CHAT_ID = -4900000004
BOT_USERNAME = "fakeassistant_bot"  # matches STATE.bot_user in fake_telegram_server.py


def main():
    h = E2EHarness(label="mention-gating")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [{"echo": True}, {"echo": True}])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.seed_room_file(GROUP_CHAT_ID, "mentions_only", "on")
        h.start_listener()

        # Not mentioned: must be ignored entirely.
        h.inject_group_message(GROUP_CHAT_ID, "what a nice day today")
        time.sleep(3)
        busy_lock = os.path.join(h.room_dir(GROUP_CHAT_ID), "busy.lock")
        replies = h.all_calls("editMessageText")
        assert not os.path.exists(busy_lock) and not replies, (
            f"un-mentioned message in a mentions_only=on group must be "
            f"ignored, but got busy.lock={os.path.exists(busy_lock)} "
            f"replies={replies}")
        print("ok: un-mentioned message ignored in mentions_only group")

        # Mentioned: must be dispatched, with the @mention stripped.
        h.inject_group_message(
            GROUP_CHAT_ID, f"@{BOT_USERNAME} what a nice day today",
            message_id=2)
        reply = h.wait_for_call(
            "editMessageText",
            lambda p: "nice day" in p.get("text", ""), timeout=20)
        text = reply["params"].get("text", "")
        print(f"ok: mentioned message was dispatched: {text!r}")

        assert f"@{BOT_USERNAME}" not in text, (
            f"expected the @mention to be stripped before reaching claude "
            f"(and echoed back), but it's still present: {text!r}")
        print("ok: @mention was stripped before reaching claude")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)
        state_path = f"{script_path}.state.{GROUP_CHAT_ID}"
        if os.path.exists(state_path):
            os.unlink(state_path)

    print("\nok: group @-mentions-only gating and stripping work end to end")


if __name__ == "__main__":
    main()
