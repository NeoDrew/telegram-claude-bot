#!/usr/bin/env python3
"""
test_footer_survives_429.py - regression test for turns that ended without
their final "Worked for..." footer message.

The turn-end footer ("⏱ Worked for...") is a ONE-SHOT tg_send, not a
streaming edit frame. tg_api's own docstring says a dropped call during a
Telegram 429 cooldown is fine for the edit-in-place path ("the next
successful flush catches up to the latest state"), but there IS no next
flush for a one-shot footer: if that exact call landed inside an active
cooldown window, it silently vanished forever, with no retry. Logs show
Telegram 429s every few minutes under normal load, so this isn't a rare
edge case. commit_live() already solved the identical
problem for the streamed reply's own final delivery via
_wait_for_cooldown(); the footer now does the same, with one retry.

Feeds a synthetic one-turn stream-json sequence through the real streamer
entrypoint (mod.main(), same technique as test_streamer_smoke.py), with a
fake urlopen that 429s ONLY the footer's own first sendMessage attempt
(everything else, including the streamed reply itself, succeeds
normally), and a monkeypatched time.sleep that simulates real time
passing (clears the cooldown) without an actual test-slowing delay.
Asserts the footer's SECOND attempt (the retry) reaches Telegram
successfully, proving the retry path this fix added actually fires.

Run: python3 tests/test_footer_survives_429.py
"""

import importlib.util
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def fake_stream_events():
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "The answer."}}}
    yield {"type": "stream_event", "event": {"type": "content_block_stop"}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": False, "duration_ms": 2000}


def main():
    failures = []

    api_calls = []
    fake_msg_id = [100]
    footer_attempts = [0]   # counts calls whose text starts with the footer's own emoji
    footer_successes = []   # only calls that actually got a 200-equivalent response

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
        url = req.full_url
        method_name = url.rsplit("/", 1)[-1]
        body = req.data.decode() if req.data else ""
        params = dict(urllib.parse.parse_qsl(body))
        api_calls.append((method_name, params))

        is_footer = method_name == "sendMessage" and params.get("text", "").startswith("⏱")
        if is_footer:
            footer_attempts[0] += 1
            if footer_attempts[0] == 1:
                # The footer's FIRST attempt lands inside an active 429
                # cooldown window. Everything else in this turn (the
                # placeholder, the streamed edits, the final commit)
                # succeeds normally, isolating the footer's own retry
                # logic from any unrelated cooldown-recovery elsewhere.
                err_body = json.dumps({
                    "ok": False, "error_code": 429,
                    "parameters": {"retry_after": 1},
                }).encode()
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(err_body))

        if method_name == "sendMessage":
            fake_msg_id[0] += 1
            resp = {"ok": True, "result": {"message_id": fake_msg_id[0]}}
        else:
            resp = {"ok": True, "result": True}
        if is_footer:
            footer_successes.append(params)
        return FakeResponse(json.dumps(resp).encode())

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

    # time.sleep is process-global; this test runs in its own subprocess
    # (tests/run.sh invokes each test file fresh), so patching it here
    # can't leak into other tests. Simulate "real time passed, the
    # cooldown Telegram handed out has now elapsed" without an actual
    # test-slowing delay: _wait_for_cooldown computes how long to sleep
    # from the SAME _rate_limited_until this clears, so as long as the
    # clear happens before tg_api's own next "now < _rate_limited_until"
    # check, the effect is identical to having genuinely waited it out.
    import time as _time_mod
    def fake_sleep(seconds):
        mod._rate_limited_until = 0.0
    _time_mod.sleep = fake_sleep

    mod.main()

    if footer_attempts[0] < 2:
        failures.append(f"expected at least 2 attempts at the footer send (first 429s, "
                         f"retry succeeds), only saw {footer_attempts[0]}")
    else:
        print(f"ok: the footer's send was attempted {footer_attempts[0]} times "
              "(first hit the 429, the fix retried)")

    if not footer_successes:
        failures.append("the footer never actually reached Telegram (no successful "
                         "response), the exact "
                         "'missing Worked for message' bug this test targets")
    else:
        print("ok: the turn-end footer reached Telegram despite the first "
              "attempt landing inside an active 429 cooldown")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: a 429 on the footer's own send is retried, not silently dropped")


if __name__ == "__main__":
    main()
