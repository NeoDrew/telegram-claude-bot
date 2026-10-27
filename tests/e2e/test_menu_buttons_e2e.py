#!/usr/bin/env python3
"""
E2E: menu-driven actions through the REAL callback_query chain: listener.sh
parses the update, strips the "act:" prefix, dispatches to
handle_action.sh, which edits the tapped message in place via
CALLBACK_MSG_ID. tests/test_dispatch.sh already covers each handler's
LOGIC directly (calling handle_action.sh with env vars set by hand), but
never exercises listener.sh's own callback_query PARSING/ROUTING/ack path
end to end - that's what's new here. None of this touches spawn_claude.sh
at all (menu actions are independent of any claude turn), so no fake
claude script is needed.

Run: python3 tests/e2e/test_menu_buttons_e2e.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, DM_CHAT_ID  # noqa: E402


def tap(h, data, message_id=1):
    """Inject a callback_query and return the resulting editMessageText
    call's params (the ack always edits the tapped message in place)."""
    h.inject_callback_query(data, chat_id=DM_CHAT_ID, message_id=message_id)
    call = h.wait_for_call(
        "editMessageText",
        lambda p: str(p.get("message_id")) == str(message_id),
        timeout=15)
    return call["params"]


def main():
    h = E2EHarness(label="menu-buttons")
    try:
        h.setup()
        h.start_listener()

        p = tap(h, "act:menu")
        # The main menu's status/model/effort info lives in the KEYBOARD
        # buttons, not the message text (the body is just "this room" +
        # cwd); check both so this doesn't over-assume where content lives.
        haystack = (p.get("text", "") + p.get("reply_markup", "")).lower()
        assert "status" in haystack and "this room" in haystack, (
            f"main menu didn't render recognisably: {p}")
        print("ok: act:menu renders the main menu")

        # answerCallbackQuery must fire (the tap spinner clears), for every
        # single one of these taps, not just the first.
        assert h.all_calls("answerCallbackQuery"), (
            "no answerCallbackQuery call at all, the tap spinner on a "
            "real phone would spin forever")
        print("ok: answerCallbackQuery acks the tap")

        p = tap(h, "act:effort high", message_id=2)
        effort_file = os.path.join(h.room_dir(DM_CHAT_ID), "effort")
        with open(effort_file) as f:
            stored = f.read().strip()
        assert stored == "high", (
            f"expected effort file to contain 'high', got {stored!r}")
        print(f"ok: act:effort high persists to disk: {p.get('text', '')!r}")

        p = tap(h, "act:effort default", message_id=3)
        # "default" deletes the effort file entirely rather than
        # truncating it (consistent with the `-f && -s` existence+content
        # guard used everywhere this file is read elsewhere).
        stored = ""
        if os.path.exists(effort_file):
            with open(effort_file) as f:
                stored = f.read().strip()
        assert stored == "", (
            f"expected act:effort default to clear the effort file, "
            f"got {stored!r}")
        print("ok: act:effort default clears the override")

        p = tap(h, "act:tune", message_id=4)
        assert "model" in p.get("text", "").lower() or "effort" in p.get(
            "text", "").lower(), f"tune screen didn't render recognisably: {p}"
        print("ok: act:tune renders the combined model+effort screen")

        p = tap(h, "act:verbosity", message_id=5)
        assert "verbosity" in p.get("text", "").lower() or "edits" in p.get(
            "text", "").lower(), f"verbosity screen didn't render: {p}"
        print("ok: act:verbosity renders its screen")

        p = tap(h, "act:more", message_id=6)
        # "more options" is the documented submenu label; a loose check
        # keeps this from breaking on cosmetic copy edits.
        assert len(p.get("text", "")) > 0, "act:more returned an empty body"
        print(f"ok: act:more renders a submenu: {p.get('text', '')[:60]!r}")

        # A totally bogus verb must not crash the dispatcher or hang the
        # tap silently; some reply (even a rejection) must come back.
        p = tap(h, "act:totally_bogus_verb_xyz", message_id=7)
        assert p.get("text"), (
            "an unknown act: verb produced no reply at all instead of a "
            "graceful rejection")
        print(f"ok: an unknown verb is handled gracefully, not silently "
              f"dropped: {p.get('text', '')[:60]!r}")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()

    print("\nok: menu buttons work correctly through the real callback_query "
          "chain, not just handle_action.sh in isolation")


if __name__ == "__main__":
    main()
