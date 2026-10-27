#!/usr/bin/env python3
"""
E2E: the one-time "bot is not admin" warning (scripts/spawn_claude.sh,
previously only verified with an isolated bash-block harness that stubbed
tg.sh). This runs it through the actual listener.sh -> spawn_claude.sh
pipeline.

Three cases in one file, each a fresh sandbox (the marker is meant to be
one-time-per-room, so cases must not share state):
  1. Group room, bot is NOT admin -> warning fires on first spawn, and does
     NOT fire again on a second message in the same room.
  2. Group room, bot IS admin -> no warning ever.
  3. DM (positive chat_id) -> the feature is group-only; no admin check call
     and no warning, regardless of admin status.

Run: python3 tests/e2e/test_no_admin_warning.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script  # noqa: E402

GROUP_CHAT_ID = -4900000001


def make_script():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(path, [
        {"text": "first reply"},
        {"text": "second reply"},
    ])
    return path


def case_not_admin():
    h = E2EHarness(label="no-admin-warning-not-admin")
    script_path = make_script()
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.set_chat_member(GROUP_CHAT_ID, status="member", can_change_info=False)
        h.start_listener()

        h.inject_group_message(GROUP_CHAT_ID, "hello there")
        warning = h.wait_for_call(
            "sendMessage",
            lambda p: "not admin" in p.get("text", "").lower(),
            timeout=20)
        print("ok: warning fired for non-admin group")
        print(f"  text={warning['params'].get('text', '')!r}")

        marker = os.path.join(h.room_dir(GROUP_CHAT_ID), ".admin_checked")
        assert os.path.exists(marker), "expected .admin_checked marker to exist"
        print("ok: .admin_checked marker written")

        before = len(h.all_calls("sendMessage"))
        h.inject_group_message(GROUP_CHAT_ID, "hello again", message_id=2)
        h.wait_for_call(
            "editMessageText",
            lambda p: "second reply" in p.get("text", ""), timeout=20)
        warnings_after = [c for c in h.all_calls("sendMessage")
                           if "not admin" in c["params"].get("text", "").lower()]
        assert len(warnings_after) == 1, (
            f"expected exactly 1 warning ever, got {len(warnings_after)} "
            f"(no-spam requirement violated)")
        print("ok: no duplicate warning on second message in the same room")
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


def case_is_admin():
    h = E2EHarness(label="no-admin-warning-is-admin")
    script_path = make_script()
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.set_chat_member(GROUP_CHAT_ID, status="administrator", can_change_info=True)
        h.start_listener()

        h.inject_group_message(GROUP_CHAT_ID, "hello there")
        h.wait_for_call(
            "editMessageText",
            lambda p: "first reply" in p.get("text", ""), timeout=20)

        warnings = [c for c in h.all_calls("sendMessage")
                    if "not admin" in c["params"].get("text", "").lower()]
        assert not warnings, f"admin room should never get the warning, got {warnings}"
        print("ok: admin group never gets the warning")

        marker = os.path.join(h.room_dir(GROUP_CHAT_ID), ".admin_checked")
        assert os.path.exists(marker), (
            "expected .admin_checked marker even for the admin case "
            "(one-time-EVER semantics, not one-time-per-non-admin-hit)")
        print("ok: .admin_checked marker still written for the admin case")
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


def case_dm_skipped():
    h = E2EHarness(label="no-admin-warning-dm")
    script_path = make_script()
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("hello there")
        h.wait_for_call(
            "editMessageText",
            lambda p: "first reply" in p.get("text", ""), timeout=20)

        admin_checks = h.all_calls("getChatMember")
        warnings = [c for c in h.all_calls("sendMessage")
                    if "not admin" in c["params"].get("text", "").lower()]
        assert not admin_checks, f"DM should never call getChatMember, got {admin_checks}"
        assert not warnings, f"DM should never get the warning, got {warnings}"
        print("ok: DM room skips the admin check and warning entirely")
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
        "not_admin": case_not_admin(),
        "is_admin": case_is_admin(),
        "dm_skipped": case_dm_skipped(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
