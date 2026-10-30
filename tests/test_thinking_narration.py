#!/usr/bin/env python3
"""
test_thinking_narration.py - Opus 5.5 / Fable 5.1 send the short notes written
between tool calls as thinking blocks (thinking_delta text), not text blocks.
The streamer used to drop all thinking content, so after the default switched
to Opus 5.5 every between-tool note silently vanished. Real reasoning still arrives with empty thinking text, so
it must stay invisible.

Asserts, in a group (edit-mode) chat:
  1. Non-empty thinking narration reaches Telegram.
  2. An empty thinking block (real reasoning) adds no content of its own.
  3. The final text answer still arrives intact after the tool marker.

Run: python3 tests/test_thinking_narration.py
"""

import importlib.util
import io
import json
import os
import sys
import urllib.parse

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")

NOTE = "Mapped out the build process, saving it to the README next."
FINAL = "Saved it to the README."


def se(ev):
    return {"type": "stream_event", "event": ev}


def events():
    yield se({"type": "message_start"})
    # Empty thinking block: real reasoning, never shown.
    yield se({"type": "content_block_start", "index": 0,
              "content_block": {"type": "thinking", "thinking": ""}})
    yield se({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": ""}})
    yield se({"type": "content_block_stop", "index": 0})
    # Non-empty thinking block: between-tool narration.
    yield se({"type": "content_block_start", "index": 1,
              "content_block": {"type": "thinking", "thinking": ""}})
    for part in (NOTE[:20], NOTE[20:]):
        yield se({"type": "content_block_delta", "index": 1,
                  "delta": {"type": "thinking_delta", "thinking": part}})
    yield se({"type": "content_block_stop", "index": 1})
    yield se({"type": "content_block_start", "index": 2,
              "content_block": {"type": "tool_use", "name": "Bash", "id": "t1"}})
    yield se({"type": "content_block_delta", "index": 2,
              "delta": {"type": "input_json_delta",
                        "partial_json": '{"command": "echo hi"}'}})
    yield se({"type": "content_block_stop", "index": 2})
    yield se({"type": "message_stop"})
    yield se({"type": "message_start"})
    yield se({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}})
    yield se({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": FINAL}})
    yield se({"type": "content_block_stop", "index": 0})
    yield se({"type": "message_stop"})
    yield {"type": "result", "result": "ok", "is_error": False, "duration_ms": 500}


def main():
    calls = []
    mid = [100]

    class Resp:
        def __init__(self, body):
            self._b = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def fake_urlopen(req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(req.data.decode() if req.data else ""))
        calls.append((method, params))
        if method == "sendMessage":
            mid[0] += 1
            return Resp(json.dumps({"ok": True, "result": {"message_id": mid[0]}}).encode())
        return Resp(json.dumps({"ok": True, "result": True}).encode())

    import urllib.request
    urllib.request.urlopen = fake_urlopen
    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in events()) + "\n")

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "-100")
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_CHAT_ID = "-100"
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    mod._log_sent = lambda m: None
    mod.main()

    texts = [p.get("text", "") for m, p in calls
             if m in ("sendMessage", "editMessageText")]
    failures = []

    if not any(NOTE in t for t in texts):
        failures.append("the between-tool thinking narration never reached Telegram")
    else:
        print("ok: thinking narration was delivered")

    if not any(FINAL in t for t in texts):
        failures.append("the final text answer never reached Telegram")
    else:
        print("ok: final text answer was delivered")

    def expected(t):
        s = t.strip()
        # Partial streaming frames are prefixes of the real messages.
        return (not s or s == "…" or NOTE.startswith(s) or FINAL.startswith(s)
                or NOTE in s or FINAL in s or "Bash" in s
                or "Thinking" in s or "Worked for" in s)

    stray = [t for t in texts if not expected(t)]
    if stray:
        failures.append(f"unexpected content from the empty thinking block: {stray!r}")
    else:
        print("ok: the empty thinking block produced no content")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: thinking-block narration renders, empty reasoning stays hidden")


if __name__ == "__main__":
    main()
