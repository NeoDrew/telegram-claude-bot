#!/usr/bin/env python3
"""
E2E: a listener restart while MULTIPLE rooms are simultaneously in
DIFFERENT states. This is the harder, more realistic version of
test_rate_limit_survives_listener_restart.py (which only ever had one
room, parked, at restart time) and test_multi_room_isolation.py (which
never restarts the listener at all). It directly verifies a claim in
CLAUDE.md's `restart` action doc that had never actually been exercised:
"detached spawns in other rooms survive" a restart, because spawn_claude.sh
detaches into its own process group via setsid, separate from the
listener's.

Three rooms, three different states, one restart:
  - Room A (DM): a genuinely in-flight turn (a hanging tool call) at the
    moment of restart. Must survive with the SAME pid, not get killed as
    collateral damage, and must still be killable by a REAL /cancel
    dispatched through the NEW listener process afterward (kill_room_spawn
    reads the pid from busy.lock on disk, with zero dependency on which
    listener process originally launched it - this is what actually makes
    restart-survival possible by design, verified here rather than assumed).
  - Room B (group): parked on a rate-limit hold at the moment of restart.
    Must survive with the hold intact and auto-resume correctly afterward
    with zero further nudges, same invariant as the single-room version of
    this test, but now proven alongside unrelated concurrent activity in
    other rooms rather than in isolation.
  - Room C (group): untouched before the restart. A message sent to it
    AFTER the restart must work normally, proving the fresh listener
    process handles a brand-new room with zero prior state correctly.

Run: python3 tests/e2e/test_multi_room_restart_survival.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import (E2EHarness, write_fake_claude_rooms_script,  # noqa: E402
                      DM_CHAT_ID, OWNER_USER_ID)

ROOM_B = -4900000008
ROOM_C = -4900000009
RESETS_IN = 10


def main():
    h = E2EHarness(label="multi-room-restart")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_rooms_script(script_path, {
        DM_CHAT_ID: [{"hang": True}],
        ROOM_B: [{"rate_limit": {"resets_in": RESETS_IN,
                                  "rate_limit_type": "five_hour"},
                   "exit_code": 1},
                  {"text": "room B resumed after the restart"}],
        ROOM_C: [{"text": "room C works fine post-restart"}],
    })
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        # --- get all three rooms into their target states BEFORE restart ---
        h.inject_dm("start a long task", message_id=1)
        h.wait_for_call("sendMessage",
                         lambda p: "sleep 999" in p.get("text", ""),
                         timeout=20)
        room_a_lock = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
        with open(room_a_lock) as f:
            room_a_pid_before = int(f.read().strip())
        print(f"ok: room A (DM) has a genuinely in-flight tool call, "
              f"pid={room_a_pid_before}")

        h.inject_group_message(ROOM_B, "please do a long task", message_id=1)
        h.wait_for_call("sendMessage",
                         lambda p: "Rate limited" in p.get("text", ""),
                         timeout=20)
        marker_path = os.path.join(h.room_dir(ROOM_B), ".rate_limited_until")
        with open(marker_path) as f:
            resume_at_before = int(f.read().strip())
        print(f"ok: room B parked on a rate-limit hold, "
              f"{resume_at_before - time.time():.1f}s remaining")

        # --- restart the listener with all of the above still live ---
        h.restart_listener()
        print("ok: listener restarted with room A in-flight and room B parked")
        time.sleep(2)

        # Room A: same pid, still genuinely alive, not collateral damage.
        assert os.path.exists(room_a_lock), (
            "room A's busy.lock vanished across the restart, the in-flight "
            "spawn was lost")
        with open(room_a_lock) as f:
            room_a_pid_after = int(f.read().strip())
        assert room_a_pid_after == room_a_pid_before, (
            f"room A's spawn pid changed across the restart: "
            f"{room_a_pid_before} -> {room_a_pid_after} (implies it was "
            f"killed and respawned, not left running)")
        try:
            os.kill(room_a_pid_after, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert alive, (
            f"room A's spawn pid {room_a_pid_after} is not actually a "
            f"live process after the restart (setsid detachment may not "
            f"be protecting it as documented)")
        print(f"ok: room A's in-flight spawn survived the restart "
              f"UNCHANGED (pid={room_a_pid_after}, confirmed alive)")

        # Room B: hold survived untouched, no premature retry.
        with open(marker_path) as f:
            marker_after = f.read().strip()
        assert marker_after == str(resume_at_before), (
            f"room B's .rate_limited_until changed across the restart: "
            f"before={resume_at_before} after={marker_after!r}")
        print(f"ok: room B's rate-limit hold survived the restart unchanged")

        # Room C: brand new room, never touched, works normally on the
        # fresh listener process.
        h.inject_group_message(ROOM_C, "hello from a brand new room",
                                user_id=int(OWNER_USER_ID), message_id=1)
        h.wait_for_call(
            "editMessageText",
            lambda p: "room C works fine post-restart" in p.get("text", ""),
            timeout=20)
        print("ok: room C (never seen before the restart) dispatches "
              "correctly on the fresh listener process")

        # Room A: a REAL /cancel, dispatched through the NEW listener
        # process, must still be able to kill the spawn the OLD listener
        # process originally launched (kill_room_spawn reads the pid from
        # busy.lock on disk, nothing in-memory).
        h.inject_callback_query("act:cancel force", chat_id=DM_CHAT_ID)
        deadline = time.time() + 10
        while time.time() < deadline and os.path.exists(room_a_lock):
            time.sleep(0.2)
        assert not os.path.exists(room_a_lock), (
            "room A's busy.lock still present after /cancel dispatched "
            "through the NEW listener process, it can't kill a spawn "
            "launched by the OLD (pre-restart) listener process")
        try:
            os.kill(room_a_pid_after, 0)
            still_alive = True
        except ProcessLookupError:
            still_alive = False
        assert not still_alive, (
            f"room A's spawn process {room_a_pid_after} is still alive "
            f"after /cancel, the kill signal never reached it")
        print("ok: /cancel through the NEW listener process correctly "
              "kills a spawn the OLD listener process originally launched")

        # Room B: zero further nudges, must still auto-resume, proving the
        # no-nudge auto-resume mechanism survives a restart too, not just
        # the marker file itself.
        deadline = time.time() + 40
        resumed = None
        while time.time() < deadline:
            for c in h.all_calls("editMessageText"):
                t = c["params"].get("text", "")
                if "room B resumed after the restart" in t:
                    resumed = t
                    break
            if resumed:
                break
            time.sleep(0.5)
        assert resumed, (
            "room B never auto-resumed after the restart, with zero "
            "further nudges\n" + h._dump_calls() + h._dump_logs())
        print(f"ok: room B auto-resumed post-restart with zero nudges: "
              f"{resumed!r}")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        os.unlink(script_path)
        for cid in (DM_CHAT_ID, ROOM_B, ROOM_C):
            state_path = f"{script_path}.state.{cid}"
            if os.path.exists(state_path):
                os.unlink(state_path)

    print("\nok: multiple rooms in different states all survive a single "
          "listener restart correctly, independently")


if __name__ == "__main__":
    main()
