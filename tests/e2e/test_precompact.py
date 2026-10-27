#!/usr/bin/env python3
"""
E2E: a message that arrives when the context is nearly full is compressed
BEFORE Claude sees it, so the chat reads: message, compression, answer.

BUG FIXED: at about 97% Claude Code rejected the next message outright
("Prompt is too long") instead of compacting first, so the user saw an
error, then a compression, then had to resend. The streamer now records
each turn's context fill in .ctx_pct, and listener.sh's context_full puts
/compact ahead of the next message once that reaches the room's
autocompact setting (else BOT_PRECOMPACT_PCT, default 90).

  1. .ctx_pct at 95 (left by an earlier spawn): the first inbox line is
     /compact, the message follows, the reading is consumed, and the
     message is answered.
  2. .ctx_pct at 50: no /compact, the message goes straight in.

Run: python3 tests/e2e/test_precompact.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import E2EHarness, DM_CHAT_ID, write_fake_claude_script  # noqa: E402


def inbox_contents(h):
    raw = h.read_room_file(DM_CHAT_ID, "inbox.ndjson", default="")
    return [json.loads(l)["message"]["content"] for l in raw.splitlines() if l.strip()]


def run_case(label, ctx, expect_compact):
    h = E2EHarness(label=label)
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    turns = ([{"text": "compacted"}] if expect_compact else []) + [{"echo": True}]
    write_fake_claude_script(script_path, turns)
    try:
        h.setup(fake_claude_script=script_path, extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.seed_room_file(DM_CHAT_ID, ".ctx_pct", str(ctx))
        h.start_listener()
        h.inject_dm("what next")
        h.wait_for_call("editMessageText",
                        lambda p: "what next" in p.get("text", ""), timeout=20)
        lines = inbox_contents(h)
        if expect_compact:
            assert lines[0] == "/compact" and "what next" in lines[1], lines
            assert not os.path.exists(os.path.join(h.room_dir(DM_CHAT_ID), ".ctx_pct")), \
                "the reading should be consumed so one burst compacts once"
            print(f"ok: {ctx}% full, /compact went ahead of the message, then it was answered")
        else:
            assert "/compact" not in lines, lines
            print(f"ok: {ctx}% full, the message went straight in")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()
        os.unlink(script_path)


def main():
    results = {
        "full_context_compacts_first": run_case("precompact-full", 95, True),
        "roomy_context_untouched": run_case("precompact-roomy", 50, False),
    }
    for name, ok in results.items():
        print(("PASS " if ok else "FAIL ") + name)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
