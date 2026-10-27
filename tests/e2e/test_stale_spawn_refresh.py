#!/usr/bin/env python3
"""
E2E: dispatch()'s stale-spawn refresh (listener.sh). A persistent spawn
reads its own script text once at exec time, so an on-disk edit made after
that is invisible to it until the process dies and restarts, which
normally only happens via a crash, rate limit, or manual /cancel. Nothing
was forcing that to happen, so a room's spawn could keep running whatever bugs existed when it started,
for as long as it happened to stay alive, regardless of how many fixes
shipped in the meantime.

dispatch() now compares a live spawn's .spawn_started_at (spawn_claude.sh)
against the newest mtime across scripts/ + hooks/ (lib.sh's code_mtime())
every time a message would otherwise be injected into it, and if the code
is newer, kills it via kill_room_spawn(rd, "stale") BEFORE routing the
message, so the reply comes from a fresh process running current code.
This must be invisible: no "cancelled" badge (that marker is user-facing,
reserved for an actual /cancel), session_id preserved, and it must not
count toward the crash-loop circuit breaker (it isn't a crash).

A second regression covered here: the check used to silently never fire
for any ALREADY-RUNNING room (its own log line never appeared, even for
rooms that took several messages after later code changes). Root cause: the code
that writes .spawn_started_at was added in the SAME change as this check,
so no spawn that predates the feature has ever written one, which is
EVERY spawn that was alive when it shipped. The check read an empty
".spawn_started_at" (missing file), failed its own `=~ ^[0-9]+$` guard, and
silently skipped comparing anything, for every single pre-existing room,
forever, since none of them would ever retroactively gain that file short
of a refresh they'd already need the check to trigger. As a result a
long-lived DM spawn could keep running code from before several fixes,
hit undefined behaviour mid rate-limit classification, and silently drop
a message instead of auto-retrying it. Fixed by falling back to busy.lock's own mtime (written at the exact
moment ANY spawn launches, old-style or new) when .spawn_started_at is
absent.

Three cases:
  1. A spawn whose .spawn_started_at predates the (sandbox-copied) code
     gets transparently refreshed on the next message: different pid
     answers it, same session_id, no cancelled badge, .crash_times
     untouched.
  2. A spawn started AFTER the code (the ordinary case; this is what
     literally every other e2e test in this suite already exercises
     without incident) is left alone: same pid keeps answering.
  3. THE REGRESSION: a spawn with NO .spawn_started_at at all (simulating
     any spawn that predates the feature), whose busy.lock is old, must still be refreshed via the
     busy.lock-mtime fallback, with all the same invisibility guarantees
     as case 1.

Run: python3 tests/e2e/test_stale_spawn_refresh.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def _before_newest_code(h, by=3600):
    """An epoch `by` seconds before the newest script in the sandbox.
    setup()'s copytree keeps the repo's original mtimes, so "an hour ago"
    is only older than the code if someone edited scripts/ within the last
    hour, which made this test pass or fail depending on when it ran.
    Touching a script instead would also trip the listener's own
    auto-restart and race the test, so backdate the spawn instead."""
    newest = 0
    for sub in ("scripts", "hooks"):
        for root, _dirs, files in os.walk(os.path.join(h.sandbox, sub)):
            for f in files:
                if f.endswith((".sh", ".py")):
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
    return int(min(newest or time.time(), time.time())) - by


def _stub_state(h, state):
    """Make the sandboxed spawn_state.classify() report `state`. The fake
    claude writes no transcript, so the real classify() can never say
    "idle", and the refresh only fires between turns."""
    stub = (
        "def classify(room_state_dir):\n"
        f"    return {{'state': {state!r}, 'wrapper_pid_alive': True}}\n"
        "def emoji_for_injected_state(state):\n"
        "    return '\U0001F440' if state == 'idle' else '\U0001FAE1'\n")
    with open(os.path.join(h.sandbox, "scripts", "spawn_state.py"), "w") as f:
        f.write(stub)


def _busy_pid(h):
    raw = h.read_room_file(DM_CHAT_ID, "busy.lock", default="").strip()
    return int(raw) if raw.isdigit() else None


def case_stale_spawn_refreshed_transparently():
    h = E2EHarness(label="stale-spawn-refresh")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "first reply"},
        {"text": "second reply, from the refreshed spawn"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        _stub_state(h, "idle")
        h.start_listener()

        h.inject_dm("hello there", message_id=1)
        h.wait_for_call("editMessageText",
                         lambda p: "first reply" in p.get("text", ""),
                         timeout=20)
        sid_before = h.session_id(DM_CHAT_ID)
        pid_before = _busy_pid(h)
        assert sid_before, "expected a session_id after the first turn"
        assert pid_before, "expected busy.lock to hold a live pid between turns"
        print(f"ok: first turn answered by pid={pid_before}, session={sid_before}")

        # Simulate "this spawn predates a code change": backdate its
        # recorded start time well before anything in the sandbox's
        # scripts/ (which were all just copied into place by setup(),
        # so their mtime is effectively "now").
        h.seed_room_file(DM_CHAT_ID, ".spawn_started_at",
                          str(_before_newest_code(h)))

        h.inject_dm("still there?", message_id=2)
        h.wait_for_call(
            "editMessageText",
            lambda p: "second reply, from the refreshed spawn" in p.get("text", ""),
            timeout=20)
        print("ok: follow-up message still answered normally")

        pid_after = _busy_pid(h)
        assert pid_after and pid_after != pid_before, (
            f"expected a genuinely NEW process to have handled the second "
            f"message (refresh didn't happen): pid_before={pid_before} "
            f"pid_after={pid_after}")
        print(f"ok: a fresh process (pid={pid_after}) answered; the stale "
              f"one was actually killed and replaced")

        sid_after = h.session_id(DM_CHAT_ID)
        assert sid_after == sid_before, (
            f"a transparent refresh must preserve session_id, same as a "
            f"real /cancel does: before={sid_before!r} after={sid_after!r}")
        print(f"ok: session_id preserved across the refresh ({sid_before})")

        cancelled = [c for c in h.all_calls()
                     if "cancelled" in c["params"].get("text", "").lower()]
        assert not cancelled, (
            f"a stale-spawn refresh must be invisible: no 'cancelled' "
            f"badge, that's reserved for an actual user /cancel. Got: "
            f"{cancelled}")
        print("ok: no 'cancelled' badge shown, refresh was invisible")

        crash_times = h.read_room_file(DM_CHAT_ID, ".crash_times", default="")
        assert not crash_times.strip(), (
            f"a stale-spawn refresh is not a crash and must not count "
            f"toward the crash-loop circuit breaker, got "
            f".crash_times={crash_times!r}")
        print("ok: .crash_times untouched, doesn't count as a crash")

        stale_marker = os.path.join(h.room_dir(DM_CHAT_ID), ".stale_refresh")
        assert not os.path.exists(stale_marker), (
            ".stale_refresh should be consumed (deleted) by cleanup(), "
            "not left dangling to hijack a later exit's classification")
        print("ok: .stale_refresh marker consumed, not left dangling")
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


def case_current_spawn_left_alone():
    h = E2EHarness(label="stale-spawn-current")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "first reply"},
        {"text": "second reply"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        _stub_state(h, "idle")
        h.start_listener()

        h.inject_dm("hello there", message_id=1)
        h.wait_for_call("editMessageText",
                         lambda p: "first reply" in p.get("text", ""),
                         timeout=20)
        pid_before = _busy_pid(h)

        # No backdating this time: .spawn_started_at is whatever the real
        # spawn wrote for itself moments ago, genuinely after the sandbox's
        # code was copied into place. This is the ordinary case every other
        # e2e test already exercises; asserting on it explicitly here
        # rather than just relying on those tests not having failed.
        h.inject_dm("still there?", message_id=2)
        h.wait_for_call("editMessageText",
                         lambda p: "second reply" in p.get("text", ""),
                         timeout=20)

        pid_after = _busy_pid(h)
        assert pid_after == pid_before, (
            f"an up-to-date spawn must NOT be refreshed: "
            f"pid_before={pid_before} pid_after={pid_after}")
        print(f"ok: current spawn (pid={pid_before}) handled both turns, "
              f"not refreshed")
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


def case_missing_marker_falls_back_to_busy_lock_mtime():
    h = E2EHarness(label="stale-spawn-no-marker")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "first reply"},
        {"text": "second reply, from the refreshed spawn"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        _stub_state(h, "idle")
        h.start_listener()

        h.inject_dm("hello there", message_id=1)
        h.wait_for_call("editMessageText",
                         lambda p: "first reply" in p.get("text", ""),
                         timeout=20)
        sid_before = h.session_id(DM_CHAT_ID)
        pid_before = _busy_pid(h)
        assert sid_before, "expected a session_id after the first turn"
        assert pid_before, "expected busy.lock to hold a live pid between turns"

        # Simulate a spawn that predates the whole feature: no
        # .spawn_started_at was ever written for it (the code that writes
        # it didn't exist yet when it started), and busy.lock is old.
        started_marker = os.path.join(h.room_dir(DM_CHAT_ID), ".spawn_started_at")
        assert os.path.exists(started_marker), (
            "test setup bug: the current spawn_claude.sh should have "
            "written this; if it didn't, the fallback below wouldn't be "
            "testing what it claims to")
        os.remove(started_marker)
        lock_path = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
        old_time = _before_newest_code(h)
        os.utime(lock_path, (old_time, old_time))
        print("ok: simulated a pre-feature spawn: no .spawn_started_at, "
              "backdated busy.lock")

        h.inject_dm("still there?", message_id=2)
        h.wait_for_call(
            "editMessageText",
            lambda p: "second reply, from the refreshed spawn" in p.get("text", ""),
            timeout=20)
        print("ok: follow-up message still answered normally")

        pid_after = _busy_pid(h)
        assert pid_after and pid_after != pid_before, (
            f"THE regression this test exists for: a spawn with no "
            f".spawn_started_at must still be refreshed, via the "
            f"busy.lock-mtime fallback: pid_before={pid_before} "
            f"pid_after={pid_after}")
        print(f"ok: a fresh process (pid={pid_after}) answered; the "
              f"marker-less stale spawn was still caught and replaced")

        sid_after = h.session_id(DM_CHAT_ID)
        assert sid_after == sid_before, (
            f"session_id must still be preserved via this fallback path: "
            f"before={sid_before!r} after={sid_after!r}")
        print(f"ok: session_id preserved across the fallback refresh ({sid_before})")

        cancelled = [c for c in h.all_calls()
                     if "cancelled" in c["params"].get("text", "").lower()]
        assert not cancelled, (
            f"a stale-spawn refresh must be invisible even via the "
            f"fallback path, got: {cancelled}")
        print("ok: no 'cancelled' badge shown")

        crash_times = h.read_room_file(DM_CHAT_ID, ".crash_times", default="")
        assert not crash_times.strip(), (
            f"must not count toward the crash-loop breaker, got "
            f".crash_times={crash_times!r}")
        print("ok: .crash_times untouched")
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


def case_mid_turn_spawn_not_killed():
    """BUG FIXED: the refresh used to kill a spawn that was mid-reply."""
    h = E2EHarness(label="stale-spawn-mid-turn")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "first reply"},
        {"text": "second reply, same spawn"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        _stub_state(h, "tool")
        h.start_listener()
        h.inject_dm("hello there", message_id=1)
        h.wait_for_call("editMessageText",
                         lambda p: "first reply" in p.get("text", ""), timeout=20)
        pid_before = _busy_pid(h)
        h.seed_room_file(DM_CHAT_ID, ".spawn_started_at", str(_before_newest_code(h)))
        h.inject_dm("still there?", message_id=2)
        h.wait_for_call("editMessageText",
                         lambda p: "second reply, same spawn" in p.get("text", ""), timeout=20)
        assert _busy_pid(h) == pid_before, "a mid-turn spawn was killed for a refresh"
        log = open(os.path.join(h.sandbox, "logs", "listener.log")).read()
        assert "refresh deferred" in log, "the deferral was not logged"
        print("ok: a stale spawn that is mid-turn is left running; the refresh waits")
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
        "mid_turn_spawn_not_killed": case_mid_turn_spawn_not_killed(),
        "stale_spawn_refreshed_transparently": case_stale_spawn_refreshed_transparently(),
        "current_spawn_left_alone": case_current_spawn_left_alone(),
        "missing_marker_falls_back_to_busy_lock_mtime":
            case_missing_marker_falls_back_to_busy_lock_mtime(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
