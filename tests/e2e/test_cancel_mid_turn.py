#!/usr/bin/env python3
"""
E2E: /cancel against a genuinely in-flight turn. The fake claude's "hang"
turn emits partial output then blocks forever (signal.pause()), so this
exercises the real SIGTERM-through-the-pipeline kill path (lib.sh's
kill_room_spawn / kill_tree walking tail -> claude -> streamer), not a
turn that happens to finish before cancel arrives.

Asserts:
  - The live message gets painted "cancelled" (stream_to_telegram.py's
    SIGTERM handler + .cancel_requested marker).
  - busy.lock clears (the spawn actually died, not just got asked to).
  - inbox.ndjson and queue.jsonl are wiped (cancel.sh's documented
    behaviour: "wipes the queue + inbox").
  - A message sent AFTER the cancel starts a genuinely new turn (proves
    the room isn't left wedged).

Run: python3 tests/e2e/test_cancel_mid_turn.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def main():
    h = E2EHarness(label="cancel-mid-turn")
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
        print("ok: the hanging tool call's marker reached Telegram "
              "(genuinely in-flight, not already finished)")

        busy_lock = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
        assert os.path.exists(busy_lock), "expected busy.lock while the hang turn is live"

        # act:cancel force: the force stop (a plain tap interrupts instead, see
        # test_menu_combinations.py), which SIGTERMs the whole spawn.
        h.inject_callback_query("act:cancel force", chat_id=DM_CHAT_ID)

        h.wait_for_call(
            "editMessageText",
            lambda p: "cancelled" in p.get("text", "").lower(), timeout=15)
        print("ok: live message painted cancelled")

        deadline = time.time() + 10
        while time.time() < deadline and os.path.exists(busy_lock):
            time.sleep(0.2)
        assert not os.path.exists(busy_lock), (
            "busy.lock still present after cancel; the spawn may not have "
            "actually died (SIGTERM didn't reach the whole pipeline)")
        print("ok: busy.lock cleared, the spawn process actually died")

        inbox = h.read_room_file(DM_CHAT_ID, "inbox.ndjson", default="")
        queue = h.read_room_file(DM_CHAT_ID, "queue.jsonl", default="")
        assert inbox.strip() == "", f"expected inbox wiped after cancel, got {inbox!r}"
        assert queue.strip() == "", f"expected queue wiped after cancel, got {queue!r}"
        print("ok: inbox and queue wiped after cancel")

        # Room must not be wedged: the next message should start a real
        # fresh turn, not silently do nothing.
        h.inject_dm("are you still there", message_id=2)
        h.wait_for_call(
            "editMessageText",
            lambda p: "fresh turn after cancel" in p.get("text", ""),
            timeout=20)
        print("ok: room accepts a genuinely new turn after cancel, not wedged")

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

    print("\nok: /cancel against a genuinely in-flight turn works end to end")


if __name__ == "__main__":
    main()
