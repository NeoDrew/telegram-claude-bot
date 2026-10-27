#!/usr/bin/env python3
"""
E2E: a typed message that starts with "/" but isn't a bot menu command
(/menu, /start) must reach Claude as ordinary message text, not get
rejected with the old "Slash commands are menu-driven" nudge.

Regression test: typing a Claude Code slash command such as /loop or /goal
used to be rejected as "slash commands are menu-driven". Before the fix, listener.sh::dispatch()'s typed-slash gate
rejected EVERY non-menu slash outright, which meant there was no way to
ever type a Claude Code slash command or skill (/compact, /loop, /goal,
...) into this chat at all. The fix narrows the gate to just this bot's
own two typeable commands (/menu, /start) and lets everything else fall
through to the normal plain-text dispatch path.

Run: python3 tests/e2e/test_typed_slash_forwarded_to_claude.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def main():
    h = E2EHarness(label="typed-slash-forwarded")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "Looping now."},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0", "BOT_DEBOUNCE_WINDOW_S": "1"})
        h.start_listener()
        h.inject_dm("/loop 5m /foo")

        call = h.wait_for_call(
            "editMessageText",
            lambda p: "Looping now" in p.get("text", ""),
            timeout=25)
        print("ok: the fake claude reply came through (message wasn't rejected)")
        print(f"  text={call['params'].get('text', '')!r}")

        inbox = h.read_room_file(DM_CHAT_ID, "inbox.ndjson")
        if "/loop 5m /foo" not in inbox:
            raise AssertionError(
                f"expected the literal typed text '/loop 5m /foo' in "
                f"inbox.ndjson (proving it reached claude as message "
                f"content), got: {inbox!r}")
        print("ok: the literal '/loop 5m /foo' text landed in inbox.ndjson, "
              "unmangled, exactly like a no-slash message would")

        rejections = h.all_calls("sendMessage") + h.all_calls("editMessageText")
        nudges = [c for c in rejections
                  if "menu-driven" in c["params"].get("text", "")]
        if nudges:
            raise AssertionError(
                f"expected NO 'Slash commands are menu-driven' nudge for a "
                f"non-menu typed slash, got {len(nudges)}: {nudges!r}")
        print("ok: no 'Slash commands are menu-driven' rejection fired")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)

    print("\nok: a typed non-menu slash command reaches claude as plain "
          "message text instead of being rejected")


if __name__ == "__main__":
    main()
