#!/usr/bin/env python3
"""
E2E test for the idle-room message-burst debounce (listener.sh's
dispatch() idle branch + drain_queue_if_idle's .debounce_until gate).

The bug: a long message got split by Telegram into several messages, and
each fragment triggered its own conversation turn, so claude answered the
first fragment and then separately answered the second. Telegram's own client splits a long paste into several separate
messages sent back-to-back. Before this fix, each fragment landing on an
IDLE room spawned its own turn immediately: claude answered the first
fragment (a sentence cut off mid-thought) before the second fragment had
even arrived, then answered the leftover fragment as an unrelated second
turn.

Fix: an idle room no longer spawns on the very first message. It buffers
into queue.jsonl and stamps .debounce_until = now + DEBOUNCE_WINDOW_S,
refreshed by every additional message that lands before it expires (the
room stays genuinely idle the whole time). drain_queue_if_idle only
drains once that quiet period is actually reached, coalescing the whole
burst into ONE turn, reusing the exact same multi-message-join logic
already used for the busy-not-ready and rate-limit-hold queue paths.

This test fires two "message-split" fragments back to back (well inside
the debounce window) and asserts they reach claude as ONE coalesced turn,
not two separate ones, using an {"echo": true} fake-claude turn so the
reply proves exactly what claude received.

Run: python3 tests/e2e/test_idle_burst_debounce.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def main():
    h = E2EHarness(label="idle-burst-debounce")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"echo": True},   # single turn: proves both fragments arrived together
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        t0 = time.time()
        # Two fragments of what was really one long paste, sent back to
        # back, mirrors Telegram's own split-long-message behaviour.
        h.inject_dm("This is the first half of a message that got split",
                    message_id=1)
        time.sleep(0.3)   # well inside DEBOUNCE_WINDOW_S (2s default)
        h.inject_dm("and this is the second half that arrived a beat later",
                    message_id=2)

        # No spawn should fire yet; still inside the debounce window. The
        # very first outbound call for any turn is the "…"/"..." placeholder
        # (see test_basic_roundtrip.py) sent via sendMessage before any
        # real content; its absence here is the proof nothing spawned.
        time.sleep(0.5)
        early_placeholder = [c for c in h.all_calls("sendMessage")
                              if c["params"].get("text", "").strip() in ("…", "...")]
        assert not early_placeholder, (
            f"a spawn fired before the debounce window elapsed; the whole "
            f"point is to wait for the burst to finish first: {early_placeholder!r}")
        print("ok: no premature spawn while still inside the debounce window")

        # Once the window elapses, exactly ONE coalesced turn should reach
        # claude and come back as ONE reply containing BOTH fragments.
        resumed = h.wait_for_call(
            "editMessageText",
            lambda p: "first half" in p.get("text", "")
                      and "second half" in p.get("text", ""),
            timeout=15)
        print("ok: both fragments reached claude as one coalesced turn")
        print(f"  text={resumed['params'].get('text', '')!r}")

        # And it must be genuinely ONE turn, not two separate ones that
        # both happened to contain overlapping text; check the listener
        # only ever spawned once for this room (one placeholder, total).
        placeholder_hits = [c for c in h.all_calls("sendMessage")
                             if c["params"].get("text", "").strip() in ("…", "...")]
        assert len(placeholder_hits) == 1, (
            f"expected exactly one spawn (one placeholder) for the whole "
            f"burst, got {len(placeholder_hits)}: {placeholder_hits!r}")
        print(f"ok: exactly one spawn for the whole burst "
              f"(total wall time: {time.time() - t0:.1f}s)")

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

    print("\nok: a burst of messages to an idle room debounces into one "
          "coalesced turn instead of one turn per fragment")


if __name__ == "__main__":
    main()
