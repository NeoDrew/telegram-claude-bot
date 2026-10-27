#!/usr/bin/env python3
"""
E2E: a DM message sent to the bot produces a real reply through the whole
real pipeline (listener.sh -> spawn_claude.sh -> stream_to_telegram.py),
running against the fake Telegram server and fake claude binary instead of
the real backends.

This is the harness's basic smoke test: if this doesn't pass, nothing built
on top of the harness can be trusted either.

Run: python3 tests/e2e/test_basic_roundtrip.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script  # noqa: E402


def main():
    h = E2EHarness(label="basic-roundtrip")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "Hello! I am the fake assistant, nice to meet you."},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()
        h.inject_dm("Hi, how are you?")

        call = h.wait_for_call(
            "editMessageText",
            lambda p: "Hello! I am the fake assistant" in p.get("text", ""),
            timeout=25)

        print("ok: got editMessageText with the fake claude reply")
        print(f"  text={call['params'].get('text', '')!r}")

        # Sanity: the very first outbound message for this turn should be
        # the "..." placeholder, sent via sendMessage before any edits.
        placeholder = h.wait_for_call(
            "sendMessage", lambda p: p.get("text", "").strip() in ("…", "..."),
            timeout=5)
        print(f"  placeholder={placeholder['params'].get('text', '')!r}")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)

    print("\nok: basic DM roundtrip works end to end against the fakes")


if __name__ == "__main__":
    main()
