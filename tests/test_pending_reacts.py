#!/usr/bin/env python3
"""
test_pending_reacts.py - regression test: a message sent while a room was
mid-flow used to get the 🫡 "written down" reaction and keep it even after
Claude had processed and answered it; now it moves to 👍 once answered.

listener.sh's inject_to_inbox() now pushes one line to a room's
.pending_reacts file per inbox.ndjson line it writes (a JSON array of the
Telegram message_ids that inbox line represents, or [] for synthetic
injects like "/compact" (see its docstring for why this stays strictly
1:1 with inbox.ndjson). stream_to_telegram.py pops the oldest line on
every completed (non-error) turn and flips those message_ids' reaction to
👍 via setMessageReaction (✅ was the original choice but is NOT in
Telegram's curated set of valid reaction emoji, every call 400'd with
REACTION_INVALID until fixed, see stream_to_telegram.py).

This test exercises the python (consumer) side directly: feeds a
synthetic two-turn stream-json sequence through the real streamer
entrypoint (mod.main(), same technique as test_streamer_smoke.py), with
mod.REPO sandboxed to a tempdir pre-seeded with .pending_reacts entries,
and asserts:
  - a clean turn pops the oldest entry and fires setMessageReaction for
    every id in it, in order
  - an errored turn does NOT pop (mirrors .turns_done's own
    not-is_error gate, so the two counters can't desync)
  - the file drains to empty once every clean turn's entry is consumed

Run: python3 tests/test_pending_reacts.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def turn_events(text, duration_ms, is_error):
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text}}}
    yield {"type": "stream_event", "event": {"type": "content_block_stop"}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": is_error, "duration_ms": duration_ms}


def fake_stream_events():
    yield from turn_events("First answer.", 1000, False)
    yield from turn_events("Second answer.", 1500, False)
    yield from turn_events("Errored answer.", 500, True)


def main():
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        chat_id = "-100"
        room_dir = repo / "state" / "rooms" / chat_id
        room_dir.mkdir(parents=True)
        # Three pre-seeded groups, one per upcoming turn, FIFO order:
        # turn 1 -> [111], turn 2 -> [222, 333], turn 3 (errors) -> [444].
        (room_dir / ".pending_reacts").write_text("[111]\n[222, 333]\n[444]\n")

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

        mod.main()

        reacts = [p for m, p in api_calls if m == "setMessageReaction"]
        react_ids = [p.get("message_id") for p in reacts]

        if react_ids != ["111", "222", "333"]:
            failures.append(f"expected setMessageReaction for 111, then 222+333 "
                             f"(the errored 3rd turn's [444] must NOT fire), got {react_ids!r}")
        else:
            print("ok: clean turns pop their .pending_reacts entry in FIFO order "
                  "and react every id in it")

        # Telegram restricts setMessageReaction to a fixed curated emoji
        # set; anything outside it 400s with REACTION_INVALID (this exact
        # class of bug, see the module docstring). 🫡/😴/👀 are already
        # proven valid elsewhere in this codebase; 👍 is also in the
        # standard set. Pinned here so a future emoji swap can't silently
        # regress into another invalid choice without this test catching
        # that it no longer matches a known-valid one.
        VALID_REACTION_EMOJI = {"👍", "🫡", "😴", "👀", "👎", "❤", "🔥", "🎉"}
        if reacts and not all(
            json.loads(p.get("reaction", "[]"))[0].get("emoji") in VALID_REACTION_EMOJI
            for p in reacts
        ):
            failures.append(f"expected every reaction to use a known-valid Telegram "
                             f"reaction emoji, got {reacts!r}")
        else:
            print("ok: reaction payload uses a known-valid Telegram reaction emoji (👍)")

        remaining = (room_dir / ".pending_reacts").read_text()
        if remaining.strip() != "[444]":
            failures.append(f"the errored turn's own entry ([444]) must survive "
                             f"un-popped (mirrors .turns_done's not-is_error gate), "
                             f"got remaining={remaining!r}")
        else:
            print("ok: an errored turn does not pop .pending_reacts, so a later "
                  "retry of the same message still has its ids to react")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: .pending_reacts drains in step with real turns, staying honest "
          "about which messages Claude has actually answered")


if __name__ == "__main__":
    main()
