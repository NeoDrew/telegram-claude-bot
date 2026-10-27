#!/usr/bin/env python3
"""
test_pending_reacts.py - reactions follow what Claude actually read, plus
the two recoveries that hang off the same inbox tracking.

listener.sh's inject_to_inbox() writes one .pending_reacts line per
inbox.ndjson line (the Telegram message_ids that line carries, [] for
synthetic lines like "/compact" or an interrupt request), so the two files
are line-for-line parallel. spawn_claude.sh runs claude with
--replay-user-messages, which echoes each inbox line the moment it is
read, mid-turn included. The streamer matches those echoes to inbox lines:
a read line gets 👀, and every line a turn read gets 👍 when that turn's
result lands. BUG FIXED: the streamer used to pop one .pending_reacts line
per result, so a message Claude folded into a running turn kept its 🫡 for
ever and later turns' 👍 landed on the wrong messages.

Cases (each feeds synthetic stream-json through the real streamer main()
with REPO sandboxed to a tempdir):
  1. no echoes (older CLI, the e2e fake): one line per clean result, an
     errored result reacts to nothing, .pending_reacts is never popped
  2. a message read mid-turn: 👀 on each echo, 👍 on both at the result
  3. "Prompt is too long": /compact plus the same message are re-queued
     once, with the message's reaction ids; a second failure gives up
     with a plain explanation, never "claude error: success"
  4. an interrupt (🛑 cancel): "Interrupted", 👍 for the cut-short message,
     marker cleared, and the next message is answered normally

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
CHAT = "-100"


def user_line(content):
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}})


def replay(content):
    return {"type": "user", "isReplay": True, "message": {"role": "user", "content": content}}


def text_turn(text):
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}
    yield {"type": "stream_event", "event": {"type": "content_block_stop"}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}


def result(is_error=False, subtype="success", text="ok"):
    return {"type": "result", "subtype": subtype, "is_error": is_error,
            "result": text, "duration_ms": 1000}


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def run(inbox, reacts, events, markers=()):
    """Run the streamer once. Returns (api_calls, room_dir)."""
    tmp = tempfile.mkdtemp()
    repo = Path(tmp)
    room = repo / "state" / "rooms" / CHAT
    room.mkdir(parents=True)
    (room / "inbox.ndjson").write_text("".join(l + "\n" for l in inbox))
    (room / ".pending_reacts").write_text("".join(json.dumps(r) + "\n" for r in reacts))
    for m in markers:
        (room / m).touch()

    calls = []
    mid = [100]

    def fake_urlopen(req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(req.data.decode() if req.data else ""))
        calls.append((method, params))
        if method == "sendMessage":
            mid[0] += 1
            return FakeResponse(json.dumps({"ok": True, "result": {"message_id": mid[0]}}).encode())
        return FakeResponse(json.dumps({"ok": True, "result": True}).encode())

    urllib.request.urlopen = fake_urlopen
    sys.stdin = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    os.environ.setdefault("TELEGRAM_CHAT_ID", CHAT)
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = repo
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = CHAT
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod.bot_config.notify_cmd = lambda: "/usr/bin/true"
    try:
        mod.main()
    except SystemExit:
        pass
    return calls, room


def reactions(calls):
    out = []
    for m, p in calls:
        if m == "setMessageReaction":
            emoji = json.loads(p["reaction"])[0]["emoji"]
            out.append((int(p["message_id"]), emoji))
    return out


def sent_texts(calls):
    return [p.get("text", "") for m, p in calls if m == "sendMessage"]


def main():
    failures = []

    def check(ok, label, detail=""):
        if ok:
            print(f"ok: {label}")
        else:
            failures.append(f"{label} {detail}")

    # 1. No echoes: one line per clean result.
    inbox = [user_line("A"), user_line("B"), user_line("C")]
    reacts = [[111], [222, 333], [444]]
    events = [*text_turn("one"), result(), *text_turn("two"), result(),
              *text_turn("err"), result(is_error=True, subtype="error_during_execution", text="")]
    calls, room = run(inbox, reacts, events)
    got = reactions(calls)
    check(got == [(111, "👍"), (222, "👍"), (333, "👍")],
          "fallback: 👍 per clean turn, nothing for the errored one", f"got {got}")
    check((room / ".turns_done").read_text().strip() == "2",
          "fallback: .turns_done counts the two answered lines")
    check((room / ".pending_reacts").read_text() == "[111]\n[222, 333]\n[444]\n",
          ".pending_reacts is indexed, never popped")

    # 2. A message read mid-turn.
    inbox = [user_line("first"), user_line("second, sent mid-turn")]
    events = [replay("first"), *text_turn("working"), replay("second, sent mid-turn"),
              *text_turn("both answered"), result()]
    calls, room = run(inbox, [[1], [2]], events)
    got = reactions(calls)
    check(got == [(1, "👀"), (2, "👀"), (1, "👍"), (2, "👍")],
          "mid-turn: 👀 when read, 👍 for both at the result", f"got {got}")
    check((room / ".turns_done").read_text().strip() == "2",
          "mid-turn: .turns_done covers both lines")

    # 3. Prompt is too long, then too long again after compacting.
    inbox = [user_line("big question")]
    events = [result(is_error=True, text="Prompt is too long"),
              result(text=""),                                   # the /compact
              result(is_error=True, text="Prompt is too long")]  # the retry
    calls, room = run(inbox, [[5]], events)
    lines = [json.loads(l)["message"]["content"]
             for l in (room / "inbox.ndjson").read_text().splitlines()]
    check(lines == ["big question", "/compact", "big question"],
          "too long: /compact and the message re-queued exactly once", f"got {lines}")
    check((room / ".pending_reacts").read_text() == "[5]\n[]\n[5]\n",
          "too long: the retry carries the message's reaction ids")
    texts = sent_texts(calls)
    check(any("Context is full" in t for t in texts), "too long: tells the user it is compressing")
    check(any("fit in the context" in t for t in texts), "too long twice: gives up with an explanation")
    check(not any("claude error" in t for t in texts),
          "too long: never 'claude error: success'", f"got {texts}")

    # 4. Interrupt.
    ctrl = json.dumps({"type": "control_request", "request_id": "c1",
                       "request": {"subtype": "interrupt"}})
    inbox = [user_line("long job"), ctrl, user_line("next")]
    events = [replay("long job"), *text_turn("halfway"),
              {"type": "control_response", "response": {"subtype": "success"}},
              result(is_error=True, subtype="error_during_execution", text=""),
              replay("next"), *text_turn("done"), result()]
    calls, room = run(inbox, [[7], [], [8]], events, markers=(".interrupt_requested",))
    texts = sent_texts(calls)
    check(any("Interrupted" in t for t in texts), "interrupt: says Interrupted")
    check(not any("claude error" in t for t in texts), "interrupt: no error message", f"got {texts}")
    check(not (room / ".interrupt_requested").exists(), "interrupt: marker cleared")
    got = [r for r in reactions(calls) if r[1] == "👍"]
    check(got == [(7, "👍"), (8, "👍")], "interrupt: both messages end on 👍", f"got {got}")
    check((room / ".turns_done").read_text().strip() == "3",
          "interrupt: .turns_done steps over the control line")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall passed")


if __name__ == "__main__":
    main()
