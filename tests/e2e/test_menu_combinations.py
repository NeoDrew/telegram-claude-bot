#!/usr/bin/env python3
"""
E2E: menu-button COMBINATIONS, not just sequential single taps (already
covered by test_menu_buttons_e2e.py). Telegram inline keyboards never
expire on their own, so a chat can genuinely have several menu screens
open at once, all independently tappable, and the same button can be
double-tapped on a slow connection. This suite exercises those shapes.

Cases:
  1. A stale, OLDER menu message is still fully tappable and edits itself
     correctly, independent of a NEWER menu screen drawn afterward. Real
     scenario: user opens /menu, gets distracted, opens /menu -> /more
     later, then goes back and taps a button on the FIRST, now-stale menu.
  2. Rapid back-to-back double-tap of the same state-changing button
     (e.g. a fumbled double-tap on a touchscreen) must not corrupt state
     or apply the change twice in a way that matters.
  3. A `decor:*` callback (inert section headers / placeholders) truly
     does nothing: acks the tap, no state change, no crash.
  4. Menu actions are NOT gated by room busy state (unlike plain text
     messages): tapping act:effort while a spawn is genuinely mid-turn
     must still persist immediately, not queue or get ignored.
  5. A bare "act:cancel" tap (no "yes") kills an in-flight spawn
     IMMEDIATELY, with no confirmation step, matching the documented
     one-tap behaviour of the `cancel` action (CLAUDE.md's action
     reference). Any arguments are ignored.

Run: python3 tests/e2e/test_menu_combinations.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def case_stale_menu_still_works():
    h = E2EHarness(label="menu-combo-stale")
    try:
        h.setup()
        h.start_listener()

        h.inject_callback_query("act:menu", chat_id=DM_CHAT_ID, message_id=1)
        h.wait_for_call("editMessageText",
                         lambda p: str(p.get("message_id")) == "1", timeout=15)
        print("ok: first menu (message 1) drawn")

        # A DIFFERENT, later screen, on a DIFFERENT message.
        h.inject_callback_query("act:more", chat_id=DM_CHAT_ID, message_id=2)
        h.wait_for_call("editMessageText",
                         lambda p: str(p.get("message_id")) == "2", timeout=15)
        print("ok: second, newer screen (message 2) drawn")

        # Now tap a button on the FIRST, now-stale message. Must still
        # work correctly and edit MESSAGE 1, not be confused by message 2
        # having been drawn since.
        h.inject_callback_query("act:effort high", chat_id=DM_CHAT_ID,
                                 message_id=1)
        call = h.wait_for_call(
            "editMessageText",
            lambda p: str(p.get("message_id")) == "1" and "high" in
            p.get("text", "").lower(), timeout=15)
        print(f"ok: stale message 1 still tappable and edits itself "
              f"correctly: {call['params'].get('text', '')!r}")

        effort_file = os.path.join(h.room_dir(DM_CHAT_ID), "effort")
        with open(effort_file) as f:
            assert f.read().strip() == "high"
        print("ok: state change from the stale message's tap persisted correctly")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()


def case_rapid_double_tap():
    h = E2EHarness(label="menu-combo-double-tap")
    try:
        h.setup()
        h.start_listener()

        # Fire both taps back to back with no wait between them, the
        # listener's single-threaded getUpdates loop will process
        # whichever ordering the fake server returns them in; there's no
        # window for a real race unless something downstream depends on
        # more than serial processing.
        h.inject_callback_query("act:effort high", chat_id=DM_CHAT_ID, message_id=1)
        h.inject_callback_query("act:effort high", chat_id=DM_CHAT_ID, message_id=1)

        deadline = time.time() + 15
        edits = []
        while time.time() < deadline:
            edits = [c for c in h.all_calls("editMessageText")
                      if "high" in c["params"].get("text", "").lower()]
            if len(edits) >= 2:
                break
            time.sleep(0.3)
        assert len(edits) >= 1, "neither tap produced any effect at all"
        print(f"ok: double-tap processed ({len(edits)} matching edit(s), "
              f"no crash)")

        effort_file = os.path.join(h.room_dir(DM_CHAT_ID), "effort")
        with open(effort_file) as f:
            stored = f.read().strip()
        assert stored == "high", (
            f"expected effort file to still cleanly read 'high' after a "
            f"double-tap, got {stored!r} (possible corruption from a "
            f"non-atomic concurrent write)")
        print("ok: no state corruption from the double-tap")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()


def case_decor_is_inert():
    h = E2EHarness(label="menu-combo-decor")
    try:
        h.setup()
        h.start_listener()

        effort_file = os.path.join(h.room_dir(DM_CHAT_ID), "effort")
        before = os.path.exists(effort_file)

        h.inject_callback_query("decor:section_header", chat_id=DM_CHAT_ID,
                                 message_id=1)
        time.sleep(2)

        # Must still ack the tap (spinner clears) even though nothing else
        # happens.
        acks = h.all_calls("answerCallbackQuery")
        assert acks, "decor: tap never got answerCallbackQuery, spinner would hang"
        print("ok: decor: tap still acks the spinner")

        edits = h.all_calls("editMessageText")
        assert not edits, (
            f"decor: is supposed to be inert, but got editMessageText "
            f"calls: {edits}")
        print("ok: decor: tap produces no message edit")

        after = os.path.exists(effort_file)
        assert before == after, "decor: tap unexpectedly touched room state"
        print("ok: decor: tap touches no room state")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()


def case_menu_action_ignores_busy_state():
    h = E2EHarness(label="menu-combo-busy")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [{"hang": True}])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("start a long task")
        h.wait_for_call("sendMessage",
                         lambda p: "sleep 999" in p.get("text", ""), timeout=20)
        busy_lock = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
        assert os.path.exists(busy_lock), "expected the room to be genuinely busy"

        # A menu action while genuinely mid-turn must still work
        # immediately, not queue behind the busy spawn the way a plain
        # text message would.
        h.inject_callback_query("act:effort xhigh", chat_id=DM_CHAT_ID,
                                 message_id=2)
        h.wait_for_call(
            "editMessageText",
            lambda p: str(p.get("message_id")) == "2"
            and "xhigh" in p.get("text", "").lower(),
            timeout=15)
        print("ok: menu action processed immediately while the room is "
              "genuinely busy, not blocked/queued behind the spawn")

        effort_file = os.path.join(h.room_dir(DM_CHAT_ID), "effort")
        with open(effort_file) as f:
            assert f.read().strip() == "xhigh"
        print("ok: state change persisted correctly while busy")

        assert os.path.exists(busy_lock), (
            "the menu action tap unexpectedly killed the in-flight spawn")
        print("ok: the in-flight spawn was undisturbed by the menu action")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()
        os.unlink(script_path)


def case_bare_cancel_has_no_confirm_step():
    h = E2EHarness(label="menu-combo-cancel-noconfirm")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [{"hang": True}])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("start a long task")
        h.wait_for_call("sendMessage",
                         lambda p: "sleep 999" in p.get("text", ""), timeout=20)
        busy_lock = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
        assert os.path.exists(busy_lock)

        # Cancel is one tap, no confirm step: bare "act:cancel" (no
        # "yes") must kill immediately, exactly like "act:cancel yes".
        # A regression toward silently requiring confirmation shows up
        # here.
        h.inject_callback_query("act:cancel", chat_id=DM_CHAT_ID, message_id=2)

        deadline = time.time() + 10
        while time.time() < deadline and os.path.exists(busy_lock):
            time.sleep(0.2)
        assert not os.path.exists(busy_lock), (
            "bare act:cancel (no 'yes') did not kill the spawn, but "
            "cancel is documented as one tap with no confirm step")
        print("ok: bare act:cancel kills immediately, no confirm step "
              "(matches the documented one-tap cancel)")
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
        "stale_menu_still_works": case_stale_menu_still_works(),
        "rapid_double_tap": case_rapid_double_tap(),
        "decor_is_inert": case_decor_is_inert(),
        "menu_action_ignores_busy_state": case_menu_action_ignores_busy_state(),
        "bare_cancel_has_no_confirm_step": case_bare_cancel_has_no_confirm_step(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
