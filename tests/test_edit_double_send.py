#!/usr/bin/env python3
"""
Regression test: Edit/Write/MultiEdit tool calls must render as exactly ONE
Telegram message when verbosity.edits == "diff" (the default), not two.

Bug recap (it showed up in group chats, which use the edit path):
content_block_stop always sent a bare "📝 *Edit* `path`" marker as its own sendMessage, regardless
of verbosity.edits. Separately, when verbosity.edits == "diff", the later
top-level "assistant" envelope ALSO sent a fuller message via
_format_edit_tool() carrying the identical "📝 *Edit* `path`" header plus the
diff body. Both fired for the same tool call, so every edit appeared twice in
the chat: once as a bare marker, once as the diff.

Fix: fold the "diff" case into the same suppress_marker gate that already
handled "off", so content_block_stop skips the bare marker for edit-family
tools whenever the fuller diff message is going to follow.

Run: python3 tests/test_edit_double_send.py
"""

import importlib.util
import io
import json
import os
import sys
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")


def edit_turn_events():
    """message_start -> tool_use (Edit, streamed input) -> content_block_stop
    -> message_stop -> a top-level "assistant" envelope carrying the full
    tool_use block (as the real claude CLI emits alongside the stream_event
    deltas) -> result."""
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "tool_1", "name": "Edit"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json":
                  '{"file_path":"/tmp/notes-log.md",'
                  '"old_string":"old line","new_string":"new line"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool_1", "name": "Edit",
         "input": {"file_path": "/tmp/notes-log.md",
                    "old_string": "old line", "new_string": "new line"}}
    ]}}
    yield {"type": "result", "result": "ok", "is_error": False}


def run_scenario():
    import urllib.request

    api_calls = []
    fake_msg_id = [200]

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

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]

    ndjson = "\n".join(json.dumps(ev) for ev in edit_turn_events()) + "\n"
    sys.stdin = io.StringIO(ndjson)

    chat_id = "-100200300"  # group -> edit path, where the bug showed up
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-token"
    os.environ["CHAT_ID"] = chat_id
    os.environ["TELEGRAM_CHAT_ID"] = chat_id
    os.environ.pop("BOT_DRAFT_STREAMING", None)

    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = chat_id
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod._log_sent = lambda mid: None
    # verbosity.edits defaults to "diff" already, but pin it explicitly so
    # this test doesn't silently stop covering the original bug if the
    # default ever changes.
    mod._verbosity = {"tool_calls": "on", "edits": "diff", "thinking": "on"}
    mod._load_verbosity = lambda: dict(mod._verbosity)

    mod.main()

    return [(m, p) for m, p in api_calls if m == "sendMessage"]


def main():
    sends = run_scenario()
    edit_sends = [p for _, p in sends
                  if "notes-log.md" in p.get("text", "")]

    print(f"sendMessage calls mentioning the edited file: {len(edit_sends)}")
    for p in edit_sends:
        print(f"  {p.get('text', '')[:100]!r}")

    failures = []
    if len(edit_sends) != 1:
        failures.append(
            f"expected exactly 1 sendMessage for the Edit call, got "
            f"{len(edit_sends)} (double-message bug regression)")
    elif "<pre>" not in edit_sends[0].get("text", ""):
        failures.append(
            "the single message sent was the bare marker, not the diff "
            "(rendered <pre> block missing), wrong message survived")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: edit tool call renders as exactly one Telegram message")


if __name__ == "__main__":
    main()
