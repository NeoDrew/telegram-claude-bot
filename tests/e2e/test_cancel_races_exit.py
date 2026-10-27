#!/usr/bin/env python3
"""
E2E: /cancel fired at the exact moment a spawn is ALSO naturally exiting on
its own (a rate limit or a crash), not against a turn that's cleanly
in-flight (see test_cancel_mid_turn.py for that scenario). This is a genuine
race between two independent paths that both react to the same exit:

  - kill_room_spawn (lib.sh) touches .user_cancel and .cancel_requested,
    THEN sends TERM. If that TERM lands before the scripted turn's own
    delay elapses, spawn_claude.sh's cleanup() TERM trap runs.
  - If the scripted turn finishes (rate_limit/crash) before TERM arrives or
    takes effect, spawn_claude.sh instead falls through to its own normal
    exit-classification code, which ALSO checks .user_cancel.

Because kill_room_spawn writes the markers BEFORE sending the signal, the
marker is on disk and fresh (per test_stale_user_cancel_marker.py's
freshness gate) via EITHER path by the time anything checks it, so
REGARDLESS of which side of the race actually wins, the outcome must be
identical: this was a user-initiated interruption, not a real crash or
rate limit, so session_id must be preserved and the marker must not leak.
fake_claude.py's `"delay"` turn field creates the window to inject the
cancel while the natural exit is still pending, instead of guessing at
timing with sleeps.

Three scenarios:
  1. Cancel races a rate-limit exit.
  2. Cancel races a genuine crash exit.
  3. Rapid double-cancel: tapping cancel twice in quick succession is a
     safe no-op the second time (kill_room_spawn's own "no lock -> return"
     guard), not a crash or a duplicate teardown.

Run: python3 tests/e2e/test_cancel_races_exit.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def _wait_for_busy_lock(h, timeout=10):
    lock_path = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(lock_path):
            return True
        time.sleep(0.05)
    return False


def _wait_for_session_id(h, timeout=10):
    """busy.lock lands the moment listener.sh backgrounds the spawn, but
    session_id isn't written until spawn_claude.sh itself starts up a beat
    later - waiting on busy.lock alone races an empty read here."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        sid = h.session_id(DM_CHAT_ID)
        if sid:
            return sid
        time.sleep(0.05)
    return None


def _assert_no_leaked_marker(h):
    marker_path = os.path.join(h.room_dir(DM_CHAT_ID), ".user_cancel")
    deadline = time.time() + 5
    while time.time() < deadline and os.path.exists(marker_path):
        time.sleep(0.1)
    assert not os.path.exists(marker_path), (
        ".user_cancel leaked past the race; whichever path handled the "
        "exit (cleanup() trap or the normal fallthrough classification) "
        "should have consumed it")


def case_cancel_races_rate_limit():
    h = E2EHarness(label="cancel-races-ratelimit")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"rate_limit": {"resets_in": 300}, "exit_code": 1, "delay": 1.5},
        {"text": "back after the race"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("please do work")
        assert _wait_for_busy_lock(h), "spawn never went busy"
        sid_before = _wait_for_session_id(h)
        assert sid_before, "expected a session_id to exist before the race"

        # Fire the cancel as early as possible in the 1.5s window, the
        # most adversarial timing, maximising the chance the TERM actually
        # lands before the scripted rate-limit delay elapses.
        h.inject_callback_query("act:cancel force", chat_id=DM_CHAT_ID)

        # Give both sides of the race time to fully resolve (TERM+trap, or
        # the natural exit + fallthrough classification) before asserting.
        deadline = time.time() + 8
        while time.time() < deadline and os.path.exists(
                os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")):
            time.sleep(0.1)
        assert not os.path.exists(
            os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")), (
            "room still shows busy long after the race should have resolved")
        print("ok: room settled after cancel raced the rate-limit exit")

        _assert_no_leaked_marker(h)
        print("ok: .user_cancel did not leak")

        sid_after = h.session_id(DM_CHAT_ID)
        assert sid_after == sid_before, (
            f"a cancel-raced exit must preserve session_id regardless of "
            f"which path won: before={sid_before!r} after={sid_after!r}")
        print(f"ok: session_id preserved across the race ({sid_before})")

        # The room must not be left wedged: a rate-limit hold should NOT be
        # stuck active (cancel must not create an unresumable park), and a
        # fresh message should get a normal reply.
        rlu_path = os.path.join(h.room_dir(DM_CHAT_ID), ".rate_limited_until")
        assert not os.path.exists(rlu_path), (
            "a cancel-raced exit left a rate-limit hold active; the room "
            "would be stuck waiting for a window that was never really hit")
        print("ok: no stray rate-limit hold left behind")

        h.inject_dm("are you still there")
        h.wait_for_call("editMessageText",
                         lambda p: "back after the race" in p.get("text", ""),
                         timeout=20)
        print("ok: room accepts a fresh message and replies normally after the race")
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    else:
        return True
    finally:
        h.teardown()
        os.unlink(script_path)
        state_path = f"{script_path}.state.{DM_CHAT_ID}"
        if os.path.exists(state_path):
            os.unlink(state_path)


def case_cancel_races_crash():
    h = E2EHarness(label="cancel-races-crash")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"crash": True, "exit_code": 1, "delay": 1.5},
        {"text": "back after the race"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("please do work")
        assert _wait_for_busy_lock(h), "spawn never went busy"
        sid_before = _wait_for_session_id(h)
        assert sid_before, "expected a session_id to exist before the race"

        h.inject_callback_query("act:cancel force", chat_id=DM_CHAT_ID)

        deadline = time.time() + 8
        while time.time() < deadline and os.path.exists(
                os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")):
            time.sleep(0.1)
        assert not os.path.exists(
            os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")), (
            "room still shows busy long after the race should have resolved")
        print("ok: room settled after cancel raced the crash exit")

        _assert_no_leaked_marker(h)
        print("ok: .user_cancel did not leak")

        sid_after = h.session_id(DM_CHAT_ID)
        assert sid_after == sid_before, (
            f"a cancel-raced crash must ALSO preserve session_id (it's a "
            f"user-initiated interruption, not a real crash loop): "
            f"before={sid_before!r} after={sid_after!r}")
        print(f"ok: session_id preserved across the race ({sid_before})")

        # A cancel-raced crash must NOT count toward the crash-loop circuit
        # breaker; it wasn't a real, unprompted failure.
        crash_times = h.read_room_file(DM_CHAT_ID, ".crash_times", default="")
        assert not crash_times.strip(), (
            f"a cancel-raced exit should not stamp .crash_times, got "
            f"{crash_times!r}; it would falsely count toward the "
            f"crash-loop circuit breaker for an interruption the user asked for")
        print("ok: .crash_times not stamped by a cancel-raced exit")

        h.inject_dm("are you still there")
        h.wait_for_call("editMessageText",
                         lambda p: "back after the race" in p.get("text", ""),
                         timeout=20)
        print("ok: room accepts a fresh message and replies normally after the race")
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    else:
        return True
    finally:
        h.teardown()
        os.unlink(script_path)
        state_path = f"{script_path}.state.{DM_CHAT_ID}"
        if os.path.exists(state_path):
            os.unlink(state_path)


def case_rapid_double_cancel():
    h = E2EHarness(label="rapid-double-cancel")
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
        h.wait_for_call("sendMessage",
                         lambda p: "sleep 999" in p.get("text", ""), timeout=20)

        # Two taps back to back, the way an impatient real user actually
        # behaves (tap again because nothing visibly happened yet), not one
        # tap with a generous pause before the second.
        h.inject_callback_query("act:cancel force", chat_id=DM_CHAT_ID, cb_id="cb1")
        h.inject_callback_query("act:cancel force", chat_id=DM_CHAT_ID, cb_id="cb2")

        h.wait_for_call("editMessageText",
                         lambda p: "cancelled" in p.get("text", "").lower(),
                         timeout=15)
        print("ok: cancelled even with a rapid double-tap")

        # The second cancel must be a quiet no-op (kill_room_spawn's own
        # "no lock -> return 0" guard), not an error and not a second
        # teardown racing the first's cleanup.
        deadline = time.time() + 5
        while time.time() < deadline and os.path.exists(
                os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")):
            time.sleep(0.1)
        assert not os.path.exists(
            os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")), (
            "busy.lock still present after a double-cancel settled")
        print("ok: room is idle and not wedged after the double-cancel")

        h.inject_dm("still there?")
        h.wait_for_call("editMessageText",
                         lambda p: "fresh turn after cancel" in p.get("text", ""),
                         timeout=20)
        print("ok: room accepts a fresh turn after a rapid double-cancel")
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    else:
        return True
    finally:
        h.teardown()
        os.unlink(script_path)


def main():
    results = {
        "cancel_races_rate_limit": case_cancel_races_rate_limit(),
        "cancel_races_crash": case_cancel_races_crash(),
        "rapid_double_cancel": case_rapid_double_cancel(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
