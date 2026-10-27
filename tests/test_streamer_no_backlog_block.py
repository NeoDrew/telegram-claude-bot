#!/usr/bin/env python3
"""
test_streamer_no_backlog_block.py - regression test for tool markers arriving
seconds after the file the tool sent (a photo could land as message N
while its own tool marker only landed as message N+1, several seconds
later, with no 429s in between).

Root cause: stream_to_telegram.py's main loop (`for line in sys.stdin`)
used to make every outbound Telegram call (maybe_flush's repeated draft/
edit updates, tool-marker sends, diff-message sends) synchronously, on the
SAME thread that reads claude's output. Each call is a real network round-
trip. Under heavy streaming (many text deltas, each potentially triggering
a real HTTP call), the cumulative time spent blocked in those calls can
exceed the time claude takes to produce more output, so the reading loop
falls behind: an event claude already emitted (like the content_block_stop
that should trigger a tool marker) might not get READ, let alone acted on,
for several real seconds, purely because the loop is still working through
a backlog of earlier calls. A tool's own side effect (e.g. Bash calling
notify-cmd to send a photo) is NOT subject to any of this (it's a
separate subprocess, independent of the streamer), so it can land in about
a second while the marker announcing it is still stuck in the backlog.

Fix: a single background sender thread now owns the actual outbound
network calls for the hot-path spots (maybe_flush, the mid-turn tool-marker
and diff-message sends). The main loop hands off a snapshot and moves
straight on to the next stdin line instead of waiting for the HTTP
round-trip.

This test proves the actual property that matters: with ONE artificially
slow outbound call in flight (simulating exactly the kind of real network
latency that used to backlog the whole loop), the streamer still reaches
and finishes processing the REST of that turn (more tool calls, more text,
the final result envelope) quickly, not gated behind the slow call. Only
main()'s own end-of-stream drain (which correctly waits for every
outstanding send before the process exits, so nothing is silently lost)
should account for the slow call's own delay.

Ordering update: the text after a slow tool marker is a NEW message, and
Telegram positions messages by arrival, so it must now wait for that marker
to land or it appears above it (messages and tool markers showing up in the
wrong order). The assertions are therefore: on-screen
(arrival) order is pre-tool text, marker, post-tool text, footer; the loop
waits for at most that one slow send, never a backlog; and everything is
drained before exit. On the pre-ordering streamer the marker lands LAST.

Run: python3 tests/test_streamer_no_backlog_block.py
"""

import importlib.util
import io
import json
import os
import sys
import time

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")

SLOW_CALL_DELAY = 1.5  # real seconds; must be long enough that "did the
                        # loop wait for this" is unambiguous against normal
                        # test execution noise (a few ms), but short enough
                        # the test suite doesn't crawl.


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
    # This tool call's OWN marker send will be made artificially slow below,
    # simulating real network latency, not a 429 (no cooldown involved).
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "name": "Bash", "id": "tool_1"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta",
                   "partial_json": '{"command": "notify-cmd --photo /tmp/shot.png"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}}
    # More work AFTER the slow tool call: this is what must NOT be
    # delayed by the slow marker send still in flight in the background.
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 2,
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 2,
        "delta": {"type": "text_delta", "text": "Sent. Here's what changed."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 2}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": False, "duration_ms": 500}


def main():
    failures = []
    fake_msg_id = [100]

    class FakeResponse:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    sends = []  # (start, end, text) for every sendMessage, i.e. every new message

    def fake_urlopen(req, timeout=None):
        import urllib.parse
        url = req.full_url
        method_name = url.rsplit("/", 1)[-1]
        body = req.data.decode() if req.data else ""
        params = dict(urllib.parse.parse_qsl(body))
        is_tool_marker = (method_name == "sendMessage"
                           and "Bash" in params.get("text", ""))
        started = time.monotonic()
        if is_tool_marker:
            # The ONE artificially slow call: real time.sleep, simulating
            # genuine network latency (not mocked away), same as the real
            # bug: no error, no 429, just a slow round-trip.
            time.sleep(SLOW_CALL_DELAY)
        if method_name == "sendMessage":
            sends.append((started, time.monotonic(), params.get("text", "")))
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
    mod._log_sent = lambda mid: None

    # Timestamp the moment the turn's own "result" envelope is processed
    # (fires synchronously on the main thread, right after all the text/
    # tool_use stream events above it have been read and acted on); this
    # is the signal that the main loop reached the END of the input,
    # independent of whether the slow background send has finished yet.
    result_processed_at = [None]
    _real_emit = mod.metrics.emit
    def wrapped_emit(event, **kw):
        if event == "turn_end":
            result_processed_at[0] = time.monotonic()
        return _real_emit(event, **kw)
    mod.metrics.emit = wrapped_emit

    t0 = time.monotonic()
    mod.main()
    t1 = time.monotonic()

    if result_processed_at[0] is None:
        failures.append("turn_end metric never fired; the result envelope "
                         "was never processed, can't measure the property "
                         "this test targets")
    else:
        time_to_reach_result = result_processed_at[0] - t0
        total_time = t1 - t0
        print(f"  time to reach+process the result envelope: {time_to_reach_result:.2f}s")
        print(f"  total mod.main() time (includes end-of-stream drain): {total_time:.2f}s")

        # Ordering fix (see _seq_take in the streamer): the next bubble
        # after a tool marker is a NEW message, so it now correctly waits
        # for that marker to land first, otherwise it lands above it. What must
        # still never happen is the original bug: waiting on a BACKLOG of
        # unrelated sends. So the loop may wait for this ONE slow send, but
        # nothing beyond it.
        if time_to_reach_result >= SLOW_CALL_DELAY + 0.75:
            failures.append(
                f"the main loop took {time_to_reach_result:.2f}s to reach the "
                f"result envelope, more than the one slow {SLOW_CALL_DELAY}s "
                f"send it is allowed to wait for: a backlog is building")
        else:
            print(f"ok: the loop reached the result in {time_to_reach_result:.2f}s, "
                  f"bounded by the single slow {SLOW_CALL_DELAY}s send, no backlog")

        # Telegram positions each new message by when its sendMessage
        # lands, so the arrival (completion) order IS the on-screen order.
        def kind(text):
            if "Bash" in text:
                return "marker"
            if "Worked for" in text:
                return "footer"
            return "bubble"
        order = [kind(x[2]) for x in sorted(sends, key=lambda x: x[1])]
        expected = ["bubble", "marker", "bubble", "footer"]
        if order != expected:
            failures.append(
                f"new messages landed in the wrong on-screen order: {order}, "
                f"expected {expected} (pre-tool text, tool marker, post-tool "
                f"text, footer)")
        else:
            print(f"ok: on-screen order is {order}")

        # And the slow call must still have actually been waited for at
        # END of stream (main()'s own drain); nothing silently dropped.
        if total_time < SLOW_CALL_DELAY * 0.9:
            failures.append(
                f"mod.main() returned in only {total_time:.2f}s total; "
                f"the slow background send doesn't appear to have been "
                f"waited for at all before the process would exit, meaning "
                f"it could be silently lost when the process exits")
        else:
            print(f"ok: mod.main() took {total_time:.2f}s total, consistent "
                  f"with correctly draining the slow send before returning")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: a slow outbound send doesn't backlog the main reading loop, "
          "but IS still waited for before the process exits")


if __name__ == "__main__":
    main()
