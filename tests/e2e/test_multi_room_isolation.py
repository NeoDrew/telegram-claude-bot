#!/usr/bin/env python3
"""
E2E: multiple rooms running real, independent, concurrent persistent claude
processes at once (CLAUDE.md: "Rooms run in parallel: room A can have a
spawn in flight while room B is idle... parallel"). Fires messages into a DM
and two separate groups interleaved (not sequentially waited-on one at a
time), each with its OWN scripted conversation via the fixture's per-room
"rooms" map, and asserts each room gets exactly its own scripted replies,
never another room's.

This is also the regression test for the turn-state design itself: earlier
in this harness's life the turn index was tracked per SCRIPT rather than
per ROOM, which meant one room's spawn could silently steal another room's
next turn. If that regressed, this test would fail with a room getting the
wrong reply.

Run: python3 tests/e2e/test_multi_room_isolation.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import (E2EHarness, write_fake_claude_rooms_script,  # noqa: E402
                      DM_CHAT_ID)

GROUP_A = -4900000006
GROUP_B = -4900000007


def main():
    h = E2EHarness(label="multi-room-isolation")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_rooms_script(script_path, {
        DM_CHAT_ID: [{"text": "DM room reply one"},
                     {"text": "DM room reply two"}],
        GROUP_A: [{"text": "Group A reply one"},
                  {"text": "Group A reply two"}],
        GROUP_B: [{"text": "Group B reply one"},
                  {"text": "Group B reply two"}],
    })
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        # Interleaved, not one-room-at-a-time: fire all three rooms' first
        # messages before waiting on any reply, so the three spawns are
        # genuinely racing each other for CPU/scheduling.
        h.inject_dm("dm first", message_id=1)
        h.inject_group_message(GROUP_A, "group a first", message_id=1)
        h.inject_group_message(GROUP_B, "group b first", message_id=1)

        h.wait_for_call("editMessageText",
                         lambda p: "DM room reply one" in p.get("text", ""),
                         timeout=25)
        h.wait_for_call("editMessageText",
                         lambda p: "Group A reply one" in p.get("text", ""),
                         timeout=25)
        h.wait_for_call("editMessageText",
                         lambda p: "Group B reply one" in p.get("text", ""),
                         timeout=25)
        print("ok: all three rooms got their own first reply, none crossed")

        # Second round, still interleaved.
        h.inject_dm("dm second", message_id=2)
        h.inject_group_message(GROUP_A, "group a second", message_id=2)
        h.inject_group_message(GROUP_B, "group b second", message_id=2)

        h.wait_for_call("editMessageText",
                         lambda p: "DM room reply two" in p.get("text", ""),
                         timeout=25)
        h.wait_for_call("editMessageText",
                         lambda p: "Group A reply two" in p.get("text", ""),
                         timeout=25)
        h.wait_for_call("editMessageText",
                         lambda p: "Group B reply two" in p.get("text", ""),
                         timeout=25)
        print("ok: all three rooms got their own second reply, still isolated")

        # Cross-contamination check: no room should ever have received
        # ANOTHER room's reply text.
        own = {
            DM_CHAT_ID: ("DM room reply",),
            str(GROUP_A): ("Group A reply",),
            str(GROUP_B): ("Group B reply",),
        }
        others = {
            DM_CHAT_ID: ("Group A reply", "Group B reply"),
            str(GROUP_A): ("DM room reply", "Group B reply"),
            str(GROUP_B): ("DM room reply", "Group A reply"),
        }
        edits = h.all_calls("editMessageText")
        for cid, bad_texts in others.items():
            room_edits = [e for e in edits
                          if str(e["params"].get("chat_id")) == str(cid)]
            for e in room_edits:
                t = e["params"].get("text", "")
                for bad in bad_texts:
                    assert bad not in t, (
                        f"room {cid} received another room's reply: "
                        f"{t!r} contains {bad!r}")
        print("ok: no cross-room contamination in any editMessageText call")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)
        for cid in (DM_CHAT_ID, GROUP_A, GROUP_B):
            state_path = f"{script_path}.state.{cid}"
            if os.path.exists(state_path):
                os.unlink(state_path)

    print("\nok: concurrent multi-room conversations stay fully isolated")


if __name__ == "__main__":
    main()
