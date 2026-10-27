#!/usr/bin/env python3
"""
test_spawn_state_reaction.py - regression test for emoji_for_injected_state()
in scripts/spawn_state.py.

Regression test: the 👀 reaction used to fire on every message sent
mid-flow, even when Claude kept running many more tool calls before
actually reading it. Root
cause: listener.sh's
dispatch() reacted 👀 to any message injected into a busy room's live
inbox, unconditionally, but claude only actually checks stdin BETWEEN
turns (a documented architectural limitation), so a message injected
while claude is deep in an unrelated tool-call chain sits unread for
however long that chain takes, sometimes 20+ more tool calls and several
minutes. 👀 promised "seen, answering at the next gap" when the truth was
"written down, whenever the current task wraps up".

Fix: only inject-react 👀 when spawn_state.classify() reports the room is
genuinely "idle" (between turns, about to check stdin imminently).
Everything else gets 🫡, the same honest "written down, will get to it"
reaction the busy-but-inbox-not-ready queue path already used.

This tests the pure mapping function directly (no transcript file I/O,
no real ~/.claude/projects dependency); classify()'s own transcript
parsing is a separate concern already exercised by /status and /rooms.

Run: python3 tests/test_spawn_state_reaction.py
"""

import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
from spawn_state import emoji_for_injected_state  # noqa: E402


def main():
    failures = []

    def check(state, expected, why):
        got = emoji_for_injected_state(state)
        if got != expected:
            failures.append(
                f"state={state!r}: expected {expected!r} ({why}), got {got!r}")
        else:
            print(f"ok: state={state!r} -> {got!r} ({why})")

    # The one case that earns the "seen" signal.
    check("idle", "👀", "genuinely between turns, next gap is imminent")

    # Every "claude is actively doing something else right now" state must
    # NOT claim 👀; this is the exact bug: these used to all get 👀
    # unconditionally just because the inbox was being tailed.
    check("tool", "🫡", "mid-tool-call, won't check stdin until it returns")
    check("streaming", "🫡", "mid-response, hasn't finished this turn yet")
    check("awaiting_result", "🫡", "waiting on the API, not between turns")

    # Degenerate/error states must fail toward the honest reaction, never
    # toward over-promising "seen".
    check("no_session", "🫡", "can't confirm idle, don't over-promise")
    check("no_spawn", "🫡", "can't confirm idle, don't over-promise")
    check("", "🫡", "classify() produced nothing usable")
    check("something_unexpected", "🫡", "unrecognized state: fail honest, not fail open to 👀")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: only a genuinely idle room earns the 👀 seen-it reaction; "
          "every mid-turn or uncertain state gets the honest 🫡 instead")


if __name__ == "__main__":
    main()
