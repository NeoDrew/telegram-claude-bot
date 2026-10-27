#!/usr/bin/env python3
"""
E2E: the 🗜 compact menu button, all three code paths in commands/compact.sh,
plus a real regression found while extending this coverage.

  1. Live injection (spawn busy, inbox ready): tapping compact injects
     "/compact" directly into the inbox and touches .compact_in_flight; when
     the streamer sees the next CLEAN result envelope, it deletes that
     marker and fires a "Context compressed." confirmation via notify-cmd.
     Also asserts the room genuinely still works afterward (a real follow-up
     message gets answered normally): the actual "nothing breaks" check.

  2. Pending while busy but the inbox isn't ready yet (the warmup/teardown
     window): touches .compact_pending instead and acks "🗜 Queued context
     compression."

  3. Idle: acks "🗜 Compressing context now." and the listener's next drain
     pass starts Claude just to compress (no message needed), so the next
     message does not wait through it. That message is then answered as an
     ordinary turn. (This used to wait for the next message and inject
     /compact BEFORE the next
     real user message once one arrives (start_spawn's pre-seed step), as a full
     round trip, confirming both the injected /compact turn and the real
     message get answered.

Real regression covered by case 1: a genuine claude result always carries
duration_ms, which stream_to_telegram.py's turn-duration footer ("⏱ Worked
for Xs") is gated on, and a manual compact's own result is no exception.
Before this fix, that footer fired a SECOND time right after "Context
compressed.", and because compaction is a huge context drop it also tripped
the footer's own independent "large drop -> auto-compact" detection, adding
a redundant "✂️ compacted 100%→7%" note. The result read as the assistant
still doing something rather than having just finished compacting.
fake_claude.py didn't support duration_ms/ctx_pct on a turn at all before
this test was written, so no existing e2e test could have caught it: every
turn's fake result silently omitted duration_ms, which meant the ENTIRE
footer code path, including the auto-compact note, was never once exercised
end to end.

A second, more serious regression: after a compact the assistant used
to take real tool calls and send real messages, not just show a
redundant footer. Root cause (case 4): a live-injected "/compact" sitting
unconsumed in the inbox when the spawn crashes or rate-limits gets pulled
into the generic crash-salvage path, which used to write it into
queue.jsonl as plain text like any other unanswered message. drain_queue_
if_idle coalesces the whole queue into ONE blob for the next spawn, so
"/compact" arrived merged with other pending content as ordinary
conversational text (losing whatever special recognition it needs to
trigger silent compaction), and claude just answered the merged text as a
real request instead. The fix re-arms .compact_pending (the same marker the
busy-not-ready/idle paths already use) instead of queuing raw text, and
clears any stale .compact_in_flight left by the dead attempt so a later,
unrelated turn can't fire a false "Context compressed."

Run: python3 tests/e2e/test_compact_flow.py
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def case_live_injection_no_redundant_footer_and_room_still_works():
    h = E2EHarness(label="compact-live")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "first normal reply"},
        # "delay" gives the test a real window to observe .compact_in_flight
        # before this turn completes and the streamer clears it. duration_ms
        # + a big ctx_pct drop reproduces a REAL compact result's shape;
        # without these the redundant-footer bug this test exists to catch
        # can never fire against the fake at all (see module docstring).
        {"text": "", "duration_ms": 25000, "ctx_pct": 7, "delay": 1.5},
        {"text": "still here after compacting"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        # A high starting ctx_pct on the first turn establishes the
        # "before" baseline the compact turn's drop is measured against.
        h.inject_dm("hello there", message_id=1)
        h.wait_for_call("editMessageText",
                         lambda p: "first normal reply" in p.get("text", ""),
                         timeout=20)
        print("ok: first turn completed, persistent spawn still alive")

        inbox_ready = os.path.join(h.room_dir(DM_CHAT_ID), ".inbox_ready")
        assert os.path.exists(inbox_ready), (
            "expected .inbox_ready present between turns (live persistent "
            "spawn) for this to exercise the LIVE compact-injection path, "
            "not the queued/pending one")

        calls_before_compact = len(h.all_calls())
        ack = h.inject_callback_query("act:compact", chat_id=DM_CHAT_ID)
        # The ack for a callback-triggered action edits the tapped
        # message in place (CALLBACK_MSG_ID), so check editMessageText.
        h.wait_for_call(
            "editMessageText",
            lambda p: "Compressing context" in p.get("text", ""),
            timeout=15)
        print("ok: compact button ack received immediately")

        flag_path = os.path.join(h.room_dir(DM_CHAT_ID), ".compact_in_flight")
        assert os.path.exists(flag_path), (
            "expected .compact_in_flight touched by the live-injection path")
        print("ok: .compact_in_flight marker set")

        inbox = h.read_room_file(DM_CHAT_ID, "inbox.ndjson", default="")
        assert '"/compact"' in inbox, (
            f"expected a literal /compact line injected into inbox.ndjson, "
            f"got {inbox!r}")
        print("ok: /compact injected directly into the live inbox")

        h.wait_for_call(
            "sendMessage",
            lambda p: "Context compressed" in p.get("text", ""),
            timeout=20)
        print("ok: 'Context compressed.' confirmation fired")

        deadline = time.time() + 10
        while time.time() < deadline and os.path.exists(flag_path):
            time.sleep(0.2)
        assert not os.path.exists(flag_path), (
            ".compact_in_flight marker left dangling after the "
            "confirmation fired, would misfire on a later unrelated turn")
        print("ok: .compact_in_flight cleared after firing (one-shot)")

        # Give the (buggy, pre-fix) footer a real chance to have fired
        # before asserting its absence; it would land within a beat of
        # the result envelope, well inside this window.
        time.sleep(1.5)
        footer_calls = [
            c for c in h.all_calls()[calls_before_compact:]
            if "Worked for" in c["params"].get("text", "")
        ]
        assert not footer_calls, (
            f"the compact turn's own duration/context footer should be "
            f"suppressed (\"Context compressed.\" already confirms it), "
            f"got a redundant one: {footer_calls}")
        print("ok: no redundant '⏱ Worked for...' footer after compacting")

        # The actual "nothing breaks" check: a genuine follow-up message
        # must still be answered normally, proving the room isn't wedged
        # and the session survived compaction.
        h.inject_dm("are you still there", message_id=2)
        h.wait_for_call(
            "editMessageText",
            lambda p: "still here after compacting" in p.get("text", ""),
            timeout=20)
        print("ok: room answers a real follow-up message normally after compacting")
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


def case_pending_when_busy_not_ready():
    h = E2EHarness(label="compact-pending-busy")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [{"text": "unused"}])
    placeholder = None
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        # Construct the narrow "busy but inbox not ready yet" window
        # directly: a real, alive process recorded in busy.lock (is_busy()
        # checks kill -0 on it), with .inbox_ready deliberately absent:
        # commands/compact.sh's own middle branch, distinct from both the
        # live-injection case (inbox_ready present) and the idle case
        # (no lock at all).
        placeholder = subprocess.Popen(["sleep", "30"])
        lock_path = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
        os.makedirs(h.room_dir(DM_CHAT_ID), exist_ok=True)
        with open(lock_path, "w") as f:
            f.write(str(placeholder.pid))
        inbox_ready = os.path.join(h.room_dir(DM_CHAT_ID), ".inbox_ready")
        assert not os.path.exists(inbox_ready), (
            "test setup bug: .inbox_ready should not exist yet")

        h.inject_callback_query("act:compact", chat_id=DM_CHAT_ID)
        h.wait_for_call(
            "editMessageText",
            lambda p: "Queued context compression" in p.get("text", ""),
            timeout=15)
        print("ok: correct ack for busy-but-not-ready ('Queued context compression.')")

        pending_path = os.path.join(h.room_dir(DM_CHAT_ID), ".compact_pending")
        assert os.path.exists(pending_path), (
            "expected .compact_pending touched for the busy-not-ready path")
        print("ok: .compact_pending marker set (not .compact_in_flight, "
              "nothing is live to inject into yet)")
        assert not os.path.exists(
            os.path.join(h.room_dir(DM_CHAT_ID), ".compact_in_flight")), (
            "the live-injection marker must NOT be set on this path")
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    else:
        return True
    finally:
        if placeholder is not None:
            try:
                placeholder.send_signal(signal.SIGKILL)
                placeholder.wait(timeout=5)
            except Exception:
                pass
        h.teardown()
        os.unlink(script_path)


def case_pending_when_idle_resolves_on_next_spawn():
    h = E2EHarness(label="compact-pending-idle")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        # The fresh spawn processes the pre-seeded "/compact" line first,
        # then the real user message: two turns, in that order.
        {"text": "", "duration_ms": 20000, "ctx_pct": 8},
        {"text": "answering your real question now"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        # An earlier conversation to compress (with none, the tap has
        # nothing to do and is dropped).
        h.seed_room_file(DM_CHAT_ID, "sessions.json", json.dumps(
            {"active_cwd": h.sandbox, "session_id": "11111111-2222-3333-4444-555555555555"}))
        # Room genuinely idle: no spawn is running, so there's no busy.lock
        # and no live inbox at all.
        busy_lock = os.path.join(h.room_dir(DM_CHAT_ID), "busy.lock")
        assert not os.path.exists(busy_lock), (
            "test setup bug: room should be idle with no prior spawn")

        h.inject_callback_query("act:compact", chat_id=DM_CHAT_ID)
        h.wait_for_call(
            "editMessageText",
            lambda p: "Compressing context now" in p.get("text", ""),
            timeout=15)
        print("ok: correct ack for idle room ('Compressing context now.')")

        # No message sent: the compaction runs on its own.
        h.wait_for_call(
            "sendMessage",
            lambda p: "Context compressed" in p.get("text", ""),
            timeout=20)
        print("ok: compressed straight away, without waiting for a message")
        pending_path = os.path.join(h.room_dir(DM_CHAT_ID), ".compact_pending")
        inbox = h.read_room_file(DM_CHAT_ID, "inbox.ndjson", default="")
        assert inbox.count("/compact") == 1 and inbox.count("\n") == 1, (
            f"the compact-only spawn should hold just the /compact line: {inbox!r}")

        h.inject_dm("what's the weather like", message_id=1)

        assert not os.path.exists(pending_path), (
            ".compact_pending left dangling; would misfire on a LATER, "
            "unrelated spawn for this room")
        print("ok: .compact_pending consumed (one-shot)")

        h.wait_for_call(
            "editMessageText",
            lambda p: "answering your real question now" in p.get("text", ""),
            timeout=20)
        print("ok: the real message that triggered the spawn is still "
              "answered normally, right after the injected /compact turn")
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


def case_compact_salvaged_after_crash_stays_separate_from_real_message():
    h = E2EHarness(label="compact-crash-salvage")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "first normal reply"},           # turn 0: "hello there"
        # "delay" gives the test a real window to observe .compact_in_flight
        # before this turn crashes and salvage clears it; without it the
        # touch-then-crash-then-clear can all happen before the test's own
        # check ever runs (same reasoning as case 1's delayed compact turn).
        {"crash": True, "exit_code": 1, "delay": 1.0},  # turn 1: consumes
                                                   # the injected "/compact"
                                                   # line, then dies before
                                                   # completing it
        {"text": "", "duration_ms": 20000, "ctx_pct": 8},  # turn 2: /compact,
                                                            # re-injected clean
                                                            # on the next spawn
        {"echo": True},                           # turn 3: the real follow-up,
                                                   # echoed back so the test can
                                                   # assert EXACTLY what reached
                                                   # claude, not just that some
                                                   # reply arrived
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        h.inject_dm("hello there", message_id=1)
        h.wait_for_call("editMessageText",
                         lambda p: "first normal reply" in p.get("text", ""),
                         timeout=20)

        inbox_ready = os.path.join(h.room_dir(DM_CHAT_ID), ".inbox_ready")
        assert os.path.exists(inbox_ready), (
            "expected the live persistent spawn to still be waiting on its "
            "inbox for this to exercise live injection, not queued/pending")

        h.inject_callback_query("act:compact", chat_id=DM_CHAT_ID)
        h.wait_for_call(
            "editMessageText",
            lambda p: "Compressing context" in p.get("text", ""),
            timeout=15)
        cif_path = os.path.join(h.room_dir(DM_CHAT_ID), ".compact_in_flight")
        assert os.path.exists(cif_path), "expected .compact_in_flight touched"
        print("ok: /compact injected live, .compact_in_flight set")

        # Consuming that injected line is scripted to crash. Confirm the
        # crash path actually fired (proves the reproduction is real, not
        # accidentally skipped).
        h.wait_for_call(
            "sendMessage", lambda p: "stopped unexpectedly" in p.get("text", "").lower(),
            timeout=20)
        print("ok: spawn crashed while /compact was still unconsumed "
              "(the regression scenario)")

        pending_path = os.path.join(h.room_dir(DM_CHAT_ID), ".compact_pending")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(pending_path):
            time.sleep(0.2)
        assert os.path.exists(pending_path), (
            "expected the crash-salvage path to re-arm .compact_pending "
            "for /compact instead of dropping it")
        print("ok: .compact_pending re-armed by salvage")

        assert not os.path.exists(cif_path), (
            ".compact_in_flight from the dead attempt must be cleared by "
            "salvage; left in place, it would fire a false 'Context "
            "compressed.' on whatever unrelated turn completes next")
        print("ok: stale .compact_in_flight cleared by salvage")

        queue = h.read_room_file(DM_CHAT_ID, "queue.jsonl", default="")
        assert "/compact" not in queue, (
            f"THE bug this test exists for: /compact must never be salvaged "
            f"into queue.jsonl as plain text (it gets coalesced with real "
            f"content into one blob for the next spawn, arriving as "
            f"ordinary conversational text instead of a real compact "
            f"trigger), got queue.jsonl={queue!r}")
        print("ok: queue.jsonl does not contain a salvaged '/compact' string")

        # Room is idle now (crash, nothing auto-queued since /compact was
        # excluded). A genuine new message is what finally resolves the
        # re-armed pending compact, same as the idle-room case.
        h.inject_dm("what do you think", message_id=2)

        h.wait_for_call(
            "sendMessage",
            lambda p: "Context compressed" in p.get("text", ""),
            timeout=20)
        print("ok: 'Context compressed.' fired from the re-armed pending compact")

        assert not os.path.exists(pending_path), (
            ".compact_pending left dangling after resolving; would "
            "misfire on a later, unrelated spawn")

        # The decisive assertion: the real message's echoed content must be
        # EXACTLY what was typed, with no trace of a merged "/compact",
        # proving the two were delivered as separate turns, not coalesced.
        echoed = h.wait_for_call(
            "editMessageText",
            lambda p: "what do you think" in p.get("text", ""),
            timeout=20)
        echoed_text = echoed["params"].get("text", "")
        assert "/compact" not in echoed_text, (
            f"the real message must reach claude on its own, with no "
            f"'/compact' merged in, got {echoed_text!r}")
        print(f"ok: real follow-up reached claude cleanly, "
              f"un-merged with /compact: {echoed_text!r}")
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
        "live_injection_no_redundant_footer_and_room_still_works":
            case_live_injection_no_redundant_footer_and_room_still_works(),
        "pending_when_busy_not_ready": case_pending_when_busy_not_ready(),
        "pending_when_idle_resolves_on_next_spawn":
            case_pending_when_idle_resolves_on_next_spawn(),
        "compact_salvaged_after_crash_stays_separate_from_real_message":
            case_compact_salvaged_after_crash_stays_separate_from_real_message(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
