#!/usr/bin/env python3
"""
E2E test for listener.sh's auto-restart-on-stale-code check
(listener_code_stale / maybe_auto_restart).

The gap: the per-room stale-spawn refresh only ever refreshed SPAWNS (a
fresh spawn re-reads spawn_claude.sh / commands/*.sh from disk on every
launch), but the LISTENER process
itself just keeps running whatever version of listener.sh it loaded at
its own last boot, with no equivalent self-check. Editing listener.sh
sat un-applied until a manual restart, every time, forever.

Fix: the listener now compares its own boot time (.listener_started_at)
against the newest mtime across scripts/+hooks/ once per main-loop
iteration, the same code_mtime() check the spawn-refresh path already
uses. If stale, it fires the exact same detached, delayed kickstart the
manual /restart menu button already uses (BOT_RESTART_CMD/BOT_RESTART_
DELAY, overridable for tests; see harness.py's mandatory safety
default: the REAL command shells out to the REAL system launchctl, so
tests must never let it fire for real).

This test overrides BOT_RESTART_CMD to a small recorder script (append
a line to a marker file) instead of a real restart, so it can verify:
  - staleness triggers the restart command exactly once
  - the cooldown prevents re-firing every loop iteration while the
    triggered kickstart is still "on its way"
  - a listener that ISN'T stale never fires it at all

It deliberately does NOT try to verify an actual process replacement:
that the real kickstart mechanism correctly restarts the listener is
already covered by test_multi_room_restart_survival.py. This test is
scoped to the NEW trigger logic itself.

Run: python3 tests/e2e/test_auto_restart_on_stale_code.py
"""

import os
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def _make_recorder(marker_path):
    fd, path = tempfile.mkstemp(suffix=".sh", prefix="restart-recorder-")
    os.close(fd)
    with open(path, "w") as f:
        f.write(f"""#!/bin/bash
date +%s%N >> "{marker_path}"
""")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _read_marker(marker_path):
    if not os.path.exists(marker_path):
        return []
    with open(marker_path) as f:
        return [l for l in f.read().splitlines() if l.strip()]


def main():
    failures = []
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [{"text": "hi"}])

    marker_fd, marker_path = tempfile.mkstemp(prefix="restart-marker-")
    os.close(marker_fd)
    os.remove(marker_path)   # recorder creates it fresh on first call
    recorder_path = _make_recorder(marker_path)

    h = E2EHarness(label="auto-restart-stale-code")
    try:
        h.setup(fake_claude_script=script_path, extra_env={
            "BOT_DRAFT_STREAMING": "0",
            "BOT_RESTART_CMD": recorder_path,
            "BOT_RESTART_DELAY": "0",
            "BOT_AUTO_RESTART_COOLDOWN_S": "10",
        })
        h.start_listener()

        # Baseline: freshly booted, code is NOT stale (the sandbox copy
        # and .listener_started_at land within the same instant), so no
        # restart should fire on its own within a normal few-second window.
        h.inject_dm("hello, are you there", message_id=1)
        h.wait_for_call(
            "editMessageText", lambda p: "hi" in p.get("text", ""), timeout=15)
        time.sleep(1.5)
        hits = _read_marker(marker_path)
        if hits:
            failures.append(
                f"restart fired with no code change at all (false positive): {hits!r}")
        else:
            print("ok: a freshly-booted listener with no code change never "
                  "auto-restarts")

        # Simulate a listener.sh edit: bump a script file's mtime well
        # past .listener_started_at.
        target = os.path.join(h.sandbox, "scripts", "listener.sh")
        future = time.time() + 5
        os.utime(target, (future, future))
        print("ok: bumped scripts/listener.sh's mtime to simulate an edit")

        # Poll for the recorder to fire; should happen within a handful
        # of loop iterations (TIMEOUT is short whenever nothing else is
        # busy/debouncing... actually it's 30s when fully idle, so this
        # also proves the check runs BEFORE the long-poll blocks, not
        # only after one completes).
        deadline = time.time() + 15
        hits = []
        while time.time() < deadline:
            hits = _read_marker(marker_path)
            if hits:
                break
            time.sleep(0.3)
        if not hits:
            failures.append(
                "restart never fired after the code was made stale; "
                "the check either isn't running or isn't detecting it")
        else:
            print(f"ok: stale code triggered the restart command "
                  f"({len(hits)} call(s) so far)")

        # Cooldown: it must not re-fire on every subsequent loop
        # iteration while still within BOT_AUTO_RESTART_COOLDOWN_S,
        # even though the file is STILL stale (nothing un-staled it;
        # a real kickstart would, but our recorder doesn't actually
        # restart anything).
        time.sleep(2)
        hits_after_cooldown_wait = _read_marker(marker_path)
        if len(hits_after_cooldown_wait) != len(hits):
            failures.append(
                f"restart re-fired while still inside the cooldown window: "
                f"expected exactly {len(hits)} call(s), got "
                f"{len(hits_after_cooldown_wait)}: {hits_after_cooldown_wait!r}")
        else:
            print(f"ok: no re-fire while inside the cooldown window "
                  f"(still exactly {len(hits)} call(s))")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        failures.append("unhandled exception, see traceback above")
    finally:
        h.teardown()
        os.unlink(script_path)
        try:
            os.unlink(script_path + ".state")
        except FileNotFoundError:
            pass
        try:
            os.unlink(recorder_path)
        except FileNotFoundError:
            pass
        try:
            os.unlink(marker_path)
        except FileNotFoundError:
            pass

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: the listener auto-restarts exactly once when its own code "
          "goes stale, and never fires spuriously otherwise")


if __name__ == "__main__":
    main()
