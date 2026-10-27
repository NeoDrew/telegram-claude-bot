#!/usr/bin/env python3
"""
E2E regression test: when the assistant deleted its room's active working
directory as part of a task (asked to delete a project folder it was also
running in), the room used to go permanently, silently dead: every
future message (and the automatic crash-retry already in flight) hit
`spawn_claude.sh`'s "active cwd does not exist" guard, which used to just
write one stderr line and exit. No notification, no recovery, and (since
that guard runs before ANY of the normal exit-classification/salvage code
further down the script) the message that triggered the failing run was
silently dropped too.

The fix mirrors how `/cd` to a genuinely different directory already works
by design (a session tied to a vanished project path can't meaningfully
resume anyway): reset active_cwd to the projects dir with a fresh session,
alert the user plainly, and re-queue the message that triggered the run so
the next automatic attempt (now pointed at a cwd that actually exists)
delivers it instead of losing it.

Run: python3 tests/e2e/test_deleted_cwd_recovery.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

REAL_DEFAULT_CWD = os.path.expanduser("~")  # BOT_PROJECTS_DIR unset in the sandbox


def main():
    h = E2EHarness(label="deleted-cwd-recovery")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "back on my feet, what can I help with"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})

        # Seed sessions.json pointing at a directory that genuinely does
        # not exist, BEFORE the listener (and therefore any spawn) starts;
        # simulates a room whose active_cwd already got deleted out from
        # under it, with a real prior session_id still on record (proving
        # the recovery path is what clears it, not just "it was already
        # empty").
        gone_dir = "/private/tmp/tcbot-e2e-gone-dir-does-not-exist"
        assert not os.path.exists(gone_dir), "test setup bug: this path must not exist"
        h.seed_room_file(DM_CHAT_ID, "sessions.json", json.dumps({
            "active_cwd": gone_dir,
            "session_id": "deadbeef-0000-0000-0000-000000000000",
        }))

        h.start_listener()
        h.inject_dm("hello there, are you around", message_id=1)

        h.wait_for_call(
            "sendMessage",
            lambda p: "no longer exists" in p.get("text", "").lower(),
            timeout=20)
        print("ok: user notified that the working directory is gone")

        sessions = h.read_room_json(DM_CHAT_ID, "sessions.json", default={})
        assert sessions.get("active_cwd") == REAL_DEFAULT_CWD, (
            f"expected active_cwd reset to the default {REAL_DEFAULT_CWD!r}, "
            f"got {sessions.get('active_cwd')!r}")
        print(f"ok: active_cwd reset to the default ({REAL_DEFAULT_CWD})")

        assert not sessions.get("session_id"), (
            f"expected session_id cleared (can't resume a session tied to "
            f"a vanished project path), got {sessions.get('session_id')!r}")
        print("ok: session_id cleared")

        # The message that triggered the failing run must not be lost;
        # it should have gone back into queue.jsonl for automatic retry.
        deadline = time.time() + 10
        queue = ""
        while time.time() < deadline:
            queue = h.read_room_file(DM_CHAT_ID, "queue.jsonl", default="")
            if "hello there" in queue:
                break
            time.sleep(0.2)
        assert "hello there" in queue, (
            f"the message that triggered this failure must be re-queued, "
            f"not silently dropped, got queue.jsonl={queue!r}")
        print("ok: the original message was re-queued, not lost")

        # And the whole point of re-queueing: the room must actually
        # recover and answer it, now that active_cwd points somewhere real.
        h.wait_for_call(
            "editMessageText",
            lambda p: "back on my feet" in p.get("text", ""),
            timeout=20)
        print("ok: room recovered and answered the re-queued message normally")
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)

    print("\nok: a deleted active_cwd is detected, recovered from, and "
          "the triggering message survives end to end")


if __name__ == "__main__":
    main()
