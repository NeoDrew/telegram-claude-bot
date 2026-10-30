#!/usr/bin/env python3
"""
test_tool_marker_no_block.py - regression test for a sent file arriving
quickly while the message announcing the command that sent it arrived
noticeably later, AFTER the file.

Root cause: commit_live() fires at every tool_use content_block_stop, not
just at true end-of-turn: it finalises the pre-tool narration text before
the tool marker message goes out. It used to call _wait_for_cooldown()
unconditionally, which blocks for up to COOLDOWN_WAIT_MAX (90s) if a 429
cooldown is active. logs/stream.log showed this firing often, with
multi-second waits. Meanwhile the TOOL
ITSELF (e.g. a Bash call to notify-cmd/tg.sh sending a photo) executes
completely independently of the streamer (Claude Code's SDK doesn't wait
for stream_to_telegram.py to finish processing an event before running the
tool), so the photo lands in seconds while the pre-tool text and tool
marker sat blocked behind a cooldown wait that had nothing to do with the
photo's own delivery.

Fix: commit_live(blocking=...): mid-turn call sites (tool_use
content_block_stop, mid-stream buffer-overflow rotation, the "diff"
verbosity edit-render path) now pass blocking=False, skipping the cooldown
wait entirely (a dropped mid-turn segment just doesn't show as its own
bubble; the tool marker and subsequent text still arrive normally in a
fresh box). True end-of-turn call sites (message_stop, the "result"
envelope, end-of-stdin, and the defensive prior-turn cleanup at
message_start) keep blocking=True; losing the actual final reply is a
much worse outcome than a brief wait.

Follow-up (a photo could still land as message N while its own tool
marker only landed as message N+1, several seconds later, with no 429s
in between): the fix above stopped
commit_live from BLOCKING on a cooldown, but the tool-marker's actual
tg_send() call was still made SYNCHRONOUSLY on the main stdin-reading loop. Under heavy streaming, that
loop's own repeated network calls (maybe_flush's draft/edit updates) could
fall behind claude's real output rate, backlogging the pipe, so a tool
marker could sit unprocessed for several real seconds even with ZERO 429s,
simply waiting its turn behind earlier queued work. Fixed by moving the
actual outbound network calls (maybe_flush's sends, the tool-marker send,
the diff-message send) onto a single background sender thread/queue; see
the module-level docstring near the top of stream_to_telegram.py for the
full design. This means the ORDERING-based check this test used to run
("no sleep recorded before the marker's OWN tg_api call fires") is no
longer meaningful: the marker's send now genuinely happens on a different
thread, on its own schedule, so it can legitimately fire AFTER an
unrelated end-of-turn wait purely due to thread scheduling; that isn't
the bug, so testing for it would make this test itself flaky/wrong.

This test feeds a synthetic turn with text narration, then a tool_use
block, then more text, then message_stop/result, with an ACTIVE 429
cooldown already in effect when the tool_use block's content_block_stop
fires. Monkeypatches _wait_for_cooldown to record every call (by reason)
without actually sleeping. Asserts:
  1. The total number of _wait_for_cooldown calls stays within the bound
     consistent with ONLY the end-of-turn commit (message_stop/result)
     being allowed to wait; if the mid-turn tool-marker commit also
     blocked, this count would be higher.
  2. The tool marker message is eventually attempted (tg_send called for
     it, after mod.main()'s own end-of-stream drain; see main()'s
     _edit_queue/_marker_queue.join() calls), even though the cooldown means it will
     fail server-side; dropping is fine, the point is not blocking.
  3. The FINAL commit (message_stop) still calls _wait_for_cooldown when
     a cooldown is active at that point; the safety property for
     end-of-turn is unchanged.
  4. The background send queue is fully drained by the time mod.main()
     returns; nothing enqueued is silently lost at process exit.

Run: python3 tests/test_tool_marker_no_block.py
"""

import importlib.util
import io
import json
import os
import sys
import time as time_mod

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def fake_stream_events():
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "Sending you the screenshot."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "name": "Bash", "id": "tool_1"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta",
                   "partial_json": '{"command": "notify-cmd --photo /tmp/shot.png"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 2,
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 2,
        "delta": {"type": "text_delta", "text": "Done."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 2}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": False, "duration_ms": 500}


def main():
    failures = []
    api_calls = []
    fake_msg_id = [100]
    sleep_calls = []       # (seconds, at_call_index) for every time.sleep invocation
    tool_marker_attempted = [False]

    class FakeResponse:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None):
        import urllib.parse
        url = req.full_url
        method_name = url.rsplit("/", 1)[-1]
        body = req.data.decode() if req.data else ""
        params = dict(urllib.parse.parse_qsl(body))
        api_calls.append((method_name, params))
        if method_name == "sendMessage" and "Bash" in params.get("text", ""):
            tool_marker_attempted[0] = True
        if method_name == "sendMessage":
            fake_msg_id[0] += 1
            resp = {"ok": True, "result": {"message_id": fake_msg_id[0]}}
        else:
            resp = {"ok": True, "result": True}
        return FakeResponse(json.dumps(resp).encode())

    import urllib.request
    urllib.request.urlopen = fake_urlopen

    ndjson = "\n".join(json.dumps(ev) for ev in fake_stream_events()) + "\n"
    sys.stdin = io.StringIO(ndjson)

    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "-100")
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = "-100"
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    mod._log_sent = lambda mid: None

    # Simulate an ACTIVE 429 cooldown for the entire run, well in the
    # future, so any blocking _wait_for_cooldown() call would sleep a
    # real, measurable amount. Real waits can run to tens of seconds; use a
    # value big enough that a test relying on real sleep would be
    # obviously slow (and we assert it's never actually slept).
    mod._rate_limited_until = time_mod.time() + 30.0

    # tg_api itself short-circuits (returns None, no urlopen call at all)
    # while _rate_limited_until is in the future; that's correct,
    # existing behaviour, not what this test is checking. Wrap tg_api to
    # record every CALL (attempted or not) so we can tell "blocking=False
    # still tries once" apart from "urlopen actually went out", which the
    # cooldown deliberately prevents for calls made before it clears.
    _real_tg_api = mod.tg_api
    def wrapped_tg_api(method, params=None, **kw):
        if method == "sendMessage" and "Bash" in (params or {}).get("text", ""):
            tool_marker_attempted[0] = True
        return _real_tg_api(method, params, **kw)
    mod.tg_api = wrapped_tg_api

    # Wrap _wait_for_cooldown directly (not time.sleep) so we know exactly
    # WHICH call site tried to block, by its own "reason" tag, a far more
    # precise signal than inferring from raw sleep() counts, which vary
    # with how many escalating retries a given branch happens to take.
    _real_wait = mod._wait_for_cooldown
    def wrapped_wait(reason="final flush"):
        sleep_calls.append(reason)
        # Don't actually block real wall-clock time in the test, and don't
        # clear _rate_limited_until either; every call in this turn stays
        # inside the same simulated cooldown window throughout.
    mod._wait_for_cooldown = wrapped_wait

    def fake_sleep(seconds):
        pass
    time_mod.sleep = fake_sleep

    mod.main()

    # main() drains both background senders before returning (see its own
    # _edit_queue.join()/_marker_queue.join() calls); by this point every
    # enqueued send, including the tool marker, has actually been attempted.
    if not mod._edit_queue.empty() or not mod._marker_queue.empty():
        failures.append("a background send queue was not fully drained when "
                         "mod.main() returned; a queued send could be lost "
                         "at real process exit")
    else:
        print("ok: both background send queues fully drained before main() returned")

    # The end-of-turn commit (message_stop / result) still must wait out an
    # active cooldown; that safety property must NOT have regressed.
    if not sleep_calls:
        failures.append("expected the final end-of-turn commit to still wait out "
                         "an active cooldown (blocking=True there), but "
                         "_wait_for_cooldown was never called at all")
    else:
        print(f"ok: the end-of-turn commit still called _wait_for_cooldown "
              f"({sleep_calls!r}), final-delivery safety preserved")

    # Precise check: exactly ONE commit_live() invocation in this scenario
    # is genuinely allowed to hit _wait_for_cooldown: the true end-of-turn
    # commit at message_stop (it lands in the "_live_id is None" salvage
    # branch here, tagged "commit_live salvage"). If the mid-turn
    # tool-marker commit regresses back to blocking=True, IT hits the exact
    # same salvage branch (its own _live_id is also None at that point in
    # this scenario) and adds a SECOND "commit_live salvage" entry; a
    # loose total-count bound doesn't reliably catch that (verified: a
    # bound of 6 let a reverted blocking=True slip through with a count of
    # only 4 vs the correct 3), so count this specific reason exactly.
    commit_live_waits = [r for r in sleep_calls if r.startswith("commit_live")]
    if len(commit_live_waits) != 1:
        failures.append(
            f"expected exactly 1 commit_live-related _wait_for_cooldown call "
            f"(the genuine end-of-turn commit's own salvage), got "
            f"{len(commit_live_waits)}: {commit_live_waits!r}; extra entries "
            f"mean the mid-turn tool-marker commit ALSO blocked")
    else:
        print(f"ok: exactly 1 commit_live-related wait ({commit_live_waits!r}): "
              f"the mid-turn tool-marker commit did not also block")

    if not tool_marker_attempted[0]:
        failures.append("the tool marker send was never even attempted; "
                         "blocking=False should still try once, just not wait first")
    else:
        print("ok: the tool marker send was attempted despite the active cooldown "
              "(no wait first, consistent with blocking=False)")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: mid-turn commit_live() calls (tool markers) don't block on an "
          "active 429 cooldown; the true end-of-turn commit still does")


if __name__ == "__main__":
    main()
