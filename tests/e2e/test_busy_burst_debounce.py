#!/usr/bin/env python3
"""
E2E test for the busy-room inject-burst debounce (listener.sh's dispatch()
busy+ready branch + drain_inject_burst's .inject_debounce_until gate).

The bug: a long numbered list, sent as several Telegram messages (Telegram
splits one long paste into multiple sends), got answered after only the
first few items arrived, with the reply treating the list as cut off.

Root cause: tests/e2e/test_idle_burst_debounce.py already covers the case
where a burst lands on a genuinely IDLE room (no live spawn at all); that
path buffers into queue.jsonl with a settle window before ever spawning.
But once a room has an ONGOING persistent conversation, `is_busy()` is
true for the room's entire lifetime (the wrapper process stays alive
between turns), so every later message goes through the OTHER branch:
inject straight into inbox.ndjson, no debounce at all. If a burst's first
fragment happens to land at the exact moment the room is sitting between
turns (spawn_state.classify() state=="idle", about to read the very next
inbox line), claude answers that fragment alone, before the sibling
fragments (arriving a beat later over the network) even reach the
inbox.

Fix: the busy+ready branch now checks classify() before injecting. If the
room is genuinely between turns, the message is buffered into
.inject_burst.jsonl / .inject_debounce_until (the same short settle
window the idle-room path uses) instead of injected immediately;
drain_inject_burst flushes the whole coalesced burst as ONE inject_to_inbox
call once the window elapses. If the room is mid-generation instead, there
is no race (claude won't read stdin again until the current turn ends), so
injection stays immediate, with no added latency for the common case.

Testing wrinkle: spawn_state.classify() reads the REAL claude session
transcript under ~/.claude/projects/, which fake_claude.py (this e2e
suite's claude stand-in) never writes; it emits synthetic stream-json
directly, it doesn't reproduce Claude Code's own on-disk transcript
format. So classify() would always see "no_session" for a sandboxed
room, and the debounce branch this test targets could never fire. Since
harness.setup() copies the real scripts/ tree into an isolated sandbox
and the sandboxed listener.sh's own REPO_ROOT resolves to that sandbox
(not this repo), overwriting the SANDBOXED copy of scripts/spawn_state.py
with a stub that unconditionally reports state=="idle" is enough to
exercise the debounce branch deterministically, without needing to
reproduce Claude Code's real transcript format. This only stubs classify()
for spawn-state PURPOSES within this one sandboxed process tree; it
doesn't touch the real scripts/spawn_state.py or any other test.

Run: python3 tests/e2e/test_busy_burst_debounce.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402

SPAWN_STATE_STUB = '''\
"""Test stub: always reports "idle" (genuinely between turns), so the
busy-room inject-burst debounce branch in dispatch() is exercised
deterministically without needing a real claude transcript on disk."""
import json
import sys


def classify(room_state_dir):
    return {
        "state": "idle", "tool_name": None, "tool_preview": None,
        "duration_s": None, "session_id": None, "transcript_age_s": None,
        "wrapper_pid_alive": True, "wrapper_pid": None,
    }


def emoji_for_injected_state(state):
    return "\\U0001F440" if state == "idle" else "\\U0001F92E"


def main():
    print(json.dumps(classify(sys.argv[1] if len(sys.argv) > 1 else None), indent=2))


if __name__ == "__main__":
    main()
'''


def main():
    h = E2EHarness(label="busy-burst-debounce")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "ack1", "duration_ms": 10},   # turn 1: warm the room up
        {"echo": True},                         # turn 2: proves what arrived
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        # Stub the SANDBOXED copy of spawn_state.py (harness.setup() already
        # copied scripts/ into h.sandbox) so classify() reports "idle"
        # unconditionally; see module docstring for why this is necessary.
        stub_path = os.path.join(h.sandbox, "scripts", "spawn_state.py")
        with open(stub_path, "w") as f:
            f.write(SPAWN_STATE_STUB)

        h.start_listener()

        h.inject_dm("kick off the conversation", message_id=1)
        h.wait_for_call(
            "editMessageText", lambda p: "ack1" in p.get("text", ""), timeout=15)
        print("ok: turn 1 completed, room is now persistent-idle between turns")

        t0 = time.time()
        h.inject_dm("This is the first half of a message that got split",
                    message_id=2)
        time.sleep(0.3)   # well inside DEBOUNCE_WINDOW_S (2s default)
        h.inject_dm("and this is the second half that arrived a beat later",
                    message_id=3)

        # No premature single-fragment answer: an editMessageText containing
        # "first half" but NOT "second half" would mean the race fired and
        # claude answered fragment 1 alone, the exact bug this test targets.
        time.sleep(2.5)
        premature = [c for c in h.all_calls("editMessageText")
                     if "first half" in c["params"].get("text", "")
                     and "second half" not in c["params"].get("text", "")]
        assert not premature, (
            f"claude answered the first fragment alone before the second "
            f"one arrived, the exact split-message bug: {premature!r}")
        print("ok: no premature answer to the first fragment alone")

        resumed = h.wait_for_call(
            "editMessageText",
            lambda p: "first half" in p.get("text", "")
                      and "second half" in p.get("text", ""),
            timeout=15)
        print("ok: both fragments reached claude as one coalesced turn")
        print(f"  text={resumed['params'].get('text', '')!r}")
        print(f"  total wall time for the burst: {time.time() - t0:.1f}s")

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

    print("\nok: a message-split burst landing between turns of an ongoing "
          "conversation debounces into one coalesced turn instead of "
          "answering the first fragment alone")


if __name__ == "__main__":
    main()
