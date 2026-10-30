#!/usr/bin/env python3
"""
Regression test for the silent-drop bug at the end of a tool call in group
chats.

Symptom: in group chats, the reply after a tool call never appeared on
Telegram, even though Claude had produced it.

Mechanism:
  In group mode (negative chat_id, no draft path), `ensure_live` opens the
  live edit-in-place box by calling `tg_send("…")`. If that sendMessage
  returns None, typically because a 429 cooldown fired after a tool-marker
  send burst, but any transient failure has the same effect, _live_id stays
  None. Every subsequent `maybe_flush` early-returns at the `if _live_id is
  None` gate, so the streamed text just accumulates in _buf. At
  `message_stop` the pre-fix `commit_live` saw _live_id is None and returned
  immediately, dropping the entire buffer. The result: tool marker visible
  in chat, post-tool reply silently lost.

  DMs don't trip this because draft mode opens the live box locally (sets
  the DRAFT_LIVE sentinel + mints a random draft_id without an API call).

The fix: in commit_live's `_live_id is None` branch, if _buf is non-empty,
wait out any active cooldown and send the buffer as a fresh message before
tearing state down.

This test simulates the exact failure: reject the first N sendMessage calls
(modelling a 429 cooldown), then assert the streamed text ultimately reaches
Telegram via the salvage path.

Run: python3 tests/test_group_silent_drop.py
"""

import importlib.util
import io
import json
import os
import sys
import urllib.parse
import urllib.error

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def tool_then_text_events():
    """A turn: thinking placeholder → tool_use → post-tool text → stop."""
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "name": "Bash"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta",
                  "partial_json": '{"command":"ls"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "text_delta",
                  "text": "Here is the complete report after the tool call: "
                          "everything checks out, all files present, "
                          "no anomalies detected."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result",
           "result": "Here is the complete report after the tool call: "
                     "everything checks out, all files present, "
                     "no anomalies detected.",
           "is_error": False}


def run_scenario(label, reject_send_until_call_n):
    """Spin up the streamer, mock Telegram so the FIRST N sendMessage calls
    after the tool marker return None (simulating a 429-induced silent drop
    of the placeholder). Returns (api_calls, final_send_bodies)."""
    import urllib.request

    api_calls = []
    msg_id = [300]
    send_call_count = [0]
    rejected_sends = [0]
    # Tool markers MUST go through cleanly (their send fires before our
    # "cooldown" window). We reject only the placeholder send that opens the
    # next live box.
    tool_marker_emitted = [False]

    class FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        body = req.data.decode() if req.data else ""
        params = dict(urllib.parse.parse_qsl(body))
        api_calls.append((method, params))

        if method == "sendMessage":
            send_call_count[0] += 1
            text = params.get("text", "")
            # Reject ONLY the placeholder "…" sends, model a transient
            # 500 (no cooldown set, so unrelated sends like the tool
            # marker and the eventual salvage proceed normally). Anything
            # else (tool markers, the salvage send carrying real content)
            # succeeds.
            is_placeholder = text == "…" or text == "&hellip;"
            if is_placeholder and rejected_sends[0] < reject_send_until_call_n:
                rejected_sends[0] += 1
                raise urllib.error.HTTPError(
                    req.full_url, 500, "Internal Server Error", {},
                    io.BytesIO(b'{"ok":false,"description":"transient"}'))
            msg_id[0] += 1
            return FakeResp(json.dumps(
                {"ok": True, "result": {"message_id": msg_id[0]}}).encode())

        # editMessageText / deleteMessage / sendMessageDraft all OK.
        return FakeResp(json.dumps({"ok": True, "result": True}).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]

    ndjson = "\n".join(json.dumps(ev) for ev in tool_then_text_events()) + "\n"
    sys.stdin = io.StringIO(ndjson)

    chat_id = "-100200300"   # negative → group → edit path
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-token"
    os.environ["CHAT_ID"] = chat_id
    os.environ["TELEGRAM_CHAT_ID"] = chat_id
    os.environ.pop("BOT_DRAFT_STREAMING", None)

    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram_silent",
        os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = chat_id
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    mod.DRAFT_INTERVAL = 0.0
    # Stub the cooldown wait so the test doesn't actually sleep, but DO
    # collapse the cooldown deadline so subsequent sends in the same call
    # path are no longer short-circuited. Production sleeps until the
    # deadline has passed; the test fast-forwards.
    def _fast_forward(reason=""):
        mod._rate_limited_until = 0.0
    mod._wait_for_cooldown = _fast_forward
    mod._log_sent = lambda mid: None

    mod.main()

    sends = [(m, p) for m, p in api_calls if m == "sendMessage"]
    return api_calls, [p.get("text", "") for _, p in sends]


def main():
    failures = []

    # Scenario: 429 rejects the FIRST placeholder send (the one
    # `ensure_live` makes when the post-tool text_delta arrives). The
    # streamer's text_delta path retries ensure_live on every delta, so
    # set the rejection budget high enough that all in-stream retries
    # fail. The salvage in commit_live must still deliver the buffer.
    api_calls, send_bodies = run_scenario("tool-then-text 429 storm",
                                          reject_send_until_call_n=20)

    print(f"\n--- tool-then-text with placeholder send 429'd ---")
    for m, p in api_calls:
        print(f"  {m}: {p.get('text', '')[:80]!r}")

    # The final reply text must reach Telegram in SOME successful send.
    delivered = any("complete report after the tool call" in b
                    for b in send_bodies)
    if not delivered:
        failures.append(
            "the post-tool reply was silently dropped, commit_live did "
            "not salvage _buf when _live_id was None. Sends seen: "
            + repr(send_bodies))

    # And the tool marker should still be there.
    tool_marker = any("Bash" in b or "⚙" in b for b in send_bodies)
    if not tool_marker:
        failures.append("tool marker missing from sends")

    # Belt-and-braces: even with NO rejection (happy path), the post-tool
    # text should reach Telegram (either via edit-in-place or send).
    api_calls2, _ = run_scenario("happy path", reject_send_until_call_n=0)
    edits = [p for m, p in api_calls2 if m == "editMessageText"]
    sends2 = [p for m, p in api_calls2 if m == "sendMessage"]
    happy_text = any("complete report" in p.get("text", "")
                     for p in edits + sends2)
    if not happy_text:
        failures.append("happy path: post-tool text never reached Telegram")

    print("\n=========================================")
    if failures:
        print(f"FAIL: {len(failures)} issue(s):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("ok: silent-drop salvage works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
