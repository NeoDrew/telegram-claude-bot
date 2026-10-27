#!/usr/bin/env python3
"""
E2E: the per-room user allowlist gate (listener.sh's sender_allowed / the
Pending-capture path). This is a security boundary: an unapproved sender's
message must never reach claude, and must be captured to pending_users.json
so the owner can approve it later. An approved non-owner user's message
must reach claude normally.

Three cases, one process each (fresh sandbox so approval state can't leak
between them):
  1. Unapproved stranger in a group -> no spawn, no reply, captured pending.
  2. Same stranger, pre-approved via seed_approved_user -> gets a real reply.
  3. The owner, in a room they've never touched before -> always allowed,
     no approval bookkeeping needed (owner is trusted everywhere).

Run: python3 tests/e2e/test_user_approval_gating.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, OWNER_USER_ID  # noqa: E402

GROUP_CHAT_ID = -4900000002
STRANGER_ID = 7000000001


def make_script():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(path, [{"text": "hello, you are approved"}])
    return path


def case_unapproved_stranger_dropped():
    h = E2EHarness(label="gating-unapproved")
    script_path = make_script()
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_group_message(GROUP_CHAT_ID, "hi, can I use the bot?",
                                user_id=STRANGER_ID, first_name="Stranger")

        # Give the listener a few real seconds to have processed the
        # update (it advances the offset either way); then assert NOTHING
        # claude-shaped happened.
        time.sleep(3)
        replies = [c for c in h.all_calls("editMessageText")
                   if "approved" in c["params"].get("text", "")]
        assert not replies, (
            f"unapproved sender's message must never reach claude, got "
            f"a reply: {replies}")
        print("ok: unapproved stranger's message was not dispatched")

        pending = h.pending_users(GROUP_CHAT_ID)
        ids = [p.get("id") for p in pending]
        assert STRANGER_ID in ids, (
            f"expected stranger captured in pending_users.json for this "
            f"room, got {pending}")
        print(f"ok: stranger captured as pending: {pending}")

        busy_lock = os.path.join(h.room_dir(GROUP_CHAT_ID), "busy.lock")
        assert not os.path.exists(busy_lock), (
            "no spawn should have started for a dropped sender")
        print("ok: no spawn was started")
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


def case_approved_stranger_dispatched():
    h = E2EHarness(label="gating-approved")
    script_path = make_script()
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.seed_approved_user(GROUP_CHAT_ID, STRANGER_ID, username="stranger")
        h.start_listener()

        h.inject_group_message(GROUP_CHAT_ID, "hi, can I use the bot?",
                                user_id=STRANGER_ID, first_name="Stranger")
        h.wait_for_call(
            "editMessageText",
            lambda p: "approved" in p.get("text", ""), timeout=20)
        print("ok: approved stranger's message WAS dispatched and answered")
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


def case_owner_always_allowed_new_room():
    h = E2EHarness(label="gating-owner-new-room")
    script_path = make_script()
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        brand_new_room = -4900000003
        h.inject_group_message(brand_new_room, "hi there, new room",
                                user_id=int(OWNER_USER_ID), first_name="Alice")
        h.wait_for_call(
            "editMessageText",
            lambda p: "approved" in p.get("text", ""), timeout=20)
        print("ok: owner is dispatched immediately in a room never "
              "approved/seen before")

        approved = h.approved_users(brand_new_room)
        assert not approved, (
            f"the owner should never be written into approved_users.json "
            f"(always allowed implicitly), got {approved}")
        print("ok: owner is not written into approved_users.json")
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
        "unapproved_stranger_dropped": case_unapproved_stranger_dropped(),
        "approved_stranger_dispatched": case_approved_stranger_dispatched(),
        "owner_always_allowed_new_room": case_owner_always_allowed_new_room(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
