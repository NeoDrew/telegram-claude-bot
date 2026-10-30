#!/usr/bin/env python3
"""
test_thinking_placeholder_429.py - the "💭 Thinking…" placeholder must not be
stranded when its deleteMessage is rate-limited.

Removing the placeholder was a single deleteMessage. When that call hit a 429
(or landed inside an active cooldown, where tg_api skips calls without trying)
the bubble stayed in the chat forever, sitting right above the turn's
"Worked for" footer. With a 429 on the placeholder delete, a group-chat
turn stranded it every time.

Fix: a failed placeholder delete is remembered and retried at end of turn,
after the cooldown clears and before the footer is sent.

Feeds a group (edit-mode) turn: thinking, a tool call, then the answer. The
first deleteMessage gets a 429. Asserts no "Thinking" bubble survives, the
answer is delivered, and the cleanup happens before the footer.

Run: python3 tests/test_thinking_placeholder_429.py
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
ANSWER = "Here is the answer."


def se(ev):
    return {"type": "stream_event", "event": ev}


def events():
    yield se({"type": "message_start"})
    yield se({"type": "content_block_start", "index": 0,
              "content_block": {"type": "thinking", "thinking": ""}})
    yield se({"type": "content_block_stop", "index": 0})
    yield se({"type": "content_block_start", "index": 1,
              "content_block": {"type": "tool_use", "name": "Bash", "id": "t1"}})
    yield se({"type": "content_block_delta", "index": 1,
              "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'}})
    yield se({"type": "content_block_stop", "index": 1})
    yield se({"type": "message_stop"})
    yield {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    yield se({"type": "message_start"})
    yield se({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}})
    yield se({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": ANSWER}})
    yield se({"type": "content_block_stop", "index": 0})
    yield se({"type": "message_stop"})
    yield {"type": "result", "subtype": "success", "is_error": False,
           "duration_ms": 1000, "result": ANSWER}


def main():
    subprocess.run = lambda *a, **k: None
    state = {}
    order = []          # (method, message_id) in call order
    deletes = [0]
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
        p = dict(urllib.parse.parse_qsl(req.data.decode() if req.data else ""))
        if method == "deleteMessage":
            deletes[0] += 1
            if deletes[0] == 1:
                body = json.dumps({"ok": False, "error_code": 429,
                                   "description": "Too Many Requests: retry after 1",
                                   "parameters": {"retry_after": 1}}).encode()
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                             {}, io.BytesIO(body))
            state.pop(int(p["message_id"]), None)
            order.append(("delete", int(p["message_id"])))
        if method == "sendMessage":
            mid[0] += 1
            state[mid[0]] = p.get("text", "")
            order.append(("send", p.get("text", "")))
            return Resp(json.dumps({"ok": True, "result": {"message_id": mid[0]}}).encode())
        if method == "editMessageText" and int(p["message_id"]) in state:
            state[int(p["message_id"])] = p.get("text", "")
        return Resp(json.dumps({"ok": True, "result": True}).encode())

    import urllib.request
    urllib.request.urlopen = fake_urlopen
    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in events()) + "\n")

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    os.environ["CHAT_ID"] = "-100"
    os.environ["TELEGRAM_CHAT_ID"] = "-100"
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_CHAT_ID = "-100"
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    mod._log_sent = lambda m: None
    mod.log = lambda m: None
    mod.main()

    failures = []
    final = [state[k] for k in sorted(state)]
    if any("Thinking" in t for t in final):
        failures.append(f"a Thinking placeholder was left in the chat: {final}")
    else:
        print("ok: no Thinking placeholder survives a rate-limited delete")

    if not any(ANSWER in t for t in final):
        failures.append(f"the answer was not delivered: {final}")
    else:
        print("ok: the answer was delivered")

    footer_at = next((i for i, (k, v) in enumerate(order)
                      if k == "send" and "Worked for" in str(v)), None)
    retried_at = max((i for i, (k, _) in enumerate(order) if k == "delete"), default=None)
    if footer_at is None or retried_at is None or retried_at > footer_at:
        failures.append("the placeholder cleanup did not happen before the footer")
    else:
        print("ok: placeholder cleaned up before the footer")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: rate-limited Thinking placeholders are retried and removed")


if __name__ == "__main__":
    main()
