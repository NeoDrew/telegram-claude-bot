#!/usr/bin/env python3
"""
test_turn_end_footer_buttons.py - regression test for the turn-end footer
("⏱ Worked for...") inline keyboard: always a Menu shortcut, plus a
Compact shortcut specifically when context is in the same >=75% warning
band as the footer's own ⚠️ emoji.

Both buttons use "act:menu_new" / "act:compact_new" (commands/menu_new.sh,
commands/compact_new.sh), not the bare "act:menu" / "act:compact" this
test originally shipped with: those edit the tapped message in place,
which destroyed this footer's own "Worked for..." text the instant either
button was tapped. The _new
verbs force a fresh message instead, leaving the footer untouched.

Feeds two synthetic one-turn stream-json sequences through the real
streamer entrypoint (mod.main(), same technique as test_streamer_smoke.py)
with mod.REPO sandboxed to a tempdir, one turn ending at low context
(no Compact button expected) and one ending at high context (Compact
button expected), and inspects the reply_markup Telegram actually received.

Run: python3 tests/test_turn_end_footer_buttons.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def result_event(duration_ms, input_tokens, context_window):
    return {
        "type": "result", "result": "ok", "is_error": False,
        "duration_ms": duration_ms,
        "modelUsage": {"claude-sonnet-5": {"contextWindow": context_window}},
        "usage": {"iterations": [{"input_tokens": input_tokens,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}]},
    }


def turn_events(text, duration_ms, input_tokens, context_window):
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text}}}
    yield {"type": "stream_event", "event": {"type": "content_block_stop"}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield result_event(duration_ms, input_tokens, context_window)


def fake_stream_events():
    # Turn 1: low context (30%): Compact button must NOT appear.
    yield from turn_events("Low-context answer.", 1000, 30_000, 100_000)
    # Turn 2: high context (80%, past the 75% warning threshold):
    # Compact button MUST appear alongside Menu.
    yield from turn_events("High-context answer.", 1500, 80_000, 100_000)


def main():
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        chat_id = "-100"
        (repo / "state" / "rooms" / chat_id).mkdir(parents=True)

        api_calls = []
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

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            method_name = url.rsplit("/", 1)[-1]
            body = req.data.decode() if req.data else ""
            params = dict(urllib.parse.parse_qsl(body))
            api_calls.append((method_name, params))
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
        os.environ.setdefault("TELEGRAM_CHAT_ID", chat_id)
        spec.loader.exec_module(mod)
        mod.REPO = repo
        mod.TELEGRAM_BOT_TOKEN = "fake-token"
        mod.TELEGRAM_CHAT_ID = chat_id
        mod.API_URL = "https://api.telegram.org/botfake-token"
        mod.EDIT_INTERVAL = 0.0
        mod.GROUP_EDIT_INTERVAL = 0.0

        mod.main()

        footers = [p for m, p in api_calls
                   if m == "sendMessage" and "Worked for" in p.get("text", "")]

        if len(footers) != 2:
            failures.append(f"expected 2 turn-end footer messages, got {len(footers)}: {footers!r}")
        else:
            low_kb = json.loads(footers[0].get("reply_markup", "{}"))
            high_kb = json.loads(footers[1].get("reply_markup", "{}"))
            low_buttons = [b["callback_data"] for row in low_kb.get("inline_keyboard", []) for b in row]
            high_buttons = [b["callback_data"] for row in high_kb.get("inline_keyboard", []) for b in row]

            if low_buttons != ["act:menu_new"]:
                failures.append(f"low-context footer: expected only [act:menu_new], got {low_buttons!r}")
            else:
                print("ok: a low-context turn-end footer gets only the Menu button")

            if high_buttons != ["act:menu_new", "act:compact_new"]:
                failures.append(f"high-context footer: expected [act:menu_new, act:compact_new], "
                                 f"got {high_buttons!r}")
            else:
                print("ok: a high-context (>=75%) turn-end footer gets Menu AND Compact")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: turn-end footer buttons match context state")


if __name__ == "__main__":
    main()
