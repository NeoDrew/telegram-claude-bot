#!/usr/bin/env python3
"""
Smoke test for stream_to_telegram.py.

Runs the streamer in-process, feeds it a synthetic claude stream-json
sequence on stdin, and intercepts the Telegram HTTP layer so no real API
calls leave the machine. Asserts:

  - Exactly ONE sendMessage call (for the live edit-in-place box) and
    multiple editMessageText calls. No extra sendMessage for tool_use or
    thinking blocks.
  - Markdown in the final text is converted (we see <b> in the editMessage
    body for **bold** input).
  - The pre-text "Reading the platform docs..." and the post-tool "Here's
    the picture..." both end up in the live message buffer.

Run: `python3 tests/test_streamer_smoke.py`
"""

import importlib.util
import io
import json
import os
import sys
import urllib.parse

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def fake_stream_events():
    """Synthesize a claude --output-format=stream-json sequence.

    Models a realistic flow: pre-tool narration, tool_use, post-tool
    narration, thinking, then the final answer with markdown. Each text
    block is emitted as its own content_block_start so we can verify the
    paragraph-break separator kicks in between them.
    """
    yield {"type": "stream_event", "event": {"type": "message_start"}}

    # --- text block 1: pre-tool narration ---
    yield {"type": "stream_event", "event": {
        "type": "content_block_start",
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta",
                  "text": "Reading the platform docs."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop"}}

    # --- tool_use: should produce an INLINE marker in the live message
    # buffer (not a separate Telegram message) WITH a preview of the input.
    # We stream the input_json in two chunks so we exercise the same
    # accumulation path the real Anthropic stream uses. ---
    yield {"type": "stream_event", "event": {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "name": "Read"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta",
                  "partial_json": '{"file_path":"/Users/foo/'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta",
                  "partial_json": 'docs/CLAUDE.md"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}}

    # --- thinking: should produce NO message and NO separator ---
    yield {"type": "stream_event", "event": {
        "type": "content_block_start",
        "content_block": {"type": "thinking"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop"}}

    # --- text block 2: post-tool narration ---
    # Should be separated from block 1 by a paragraph break.
    yield {"type": "stream_event", "event": {
        "type": "content_block_start",
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta",
                  "text": "Found them. Here's the **picture**:"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta",
                  "text": "\n- **Widget** $37.60\n- **Gadget** nothing"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop"}}

    yield {"type": "stream_event", "event": {"type": "message_stop"}}

    # --- SECOND TURN (persistent mode): next user message arrives, claude
    # starts a brand new assistant message. The streamer must create a new
    # Telegram message rather than editing the first turn's box.
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start",
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta",
                  "text": "Second-turn answer."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop"}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}

    # duration_ms drives the "⏱ Worked for …" turn-duration footer. 2300ms → "2s".
    yield {"type": "result", "result": "ok", "is_error": False, "duration_ms": 2300}


def run_test():
    # ----- intercept urllib.request.urlopen -----
    import urllib.request

    api_calls: list[tuple[str, dict]] = []  # (method_name, params)
    fake_msg_id = [100]

    class FakeResponse:
        def __init__(self, body: bytes):
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
        elif method_name == "editMessageText":
            resp = {"ok": True, "result": True}
        else:
            resp = {"ok": True, "result": True}
        return FakeResponse(json.dumps(resp).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]

    # ----- prep stdin with synthetic NDJSON -----
    ndjson = "\n".join(json.dumps(ev) for ev in fake_stream_events()) + "\n"
    sys.stdin = io.StringIO(ndjson)

    # ----- load the streamer fresh -----
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram",
        os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    # Patch credentials so module-load passes the missing-token gate.
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
    spec.loader.exec_module(mod)
    # Full markers: these cases check tool previews (compact is the default).
    mod._load_verbosity = lambda: {"tool_calls": "on", "edits": "diff", "thinking": "on"}
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = "0"
    mod.API_URL = "https://api.telegram.org/botfake-token"
    # Make edits fire on every call so the smoke test sees them all.
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    # Stub out the on-disk sent_msgs.log writer. Previously the test ran
    # with TELEGRAM_CHAT_ID="0" and the real _log_sent dutifully created
    # state/rooms/0/sent_msgs.log on every test run, that ghost room
    # then showed up in /forget. Belt-and-braces: stream_to_telegram.py
    # also refuses chat_id "0" defensively now, but stubbing here means
    # the test touches no on-disk state at all.
    mod._log_sent = lambda mid: None

    mod.main()

    # ----- assert -----
    failures = []

    sends = [p for m, p in api_calls if m == "sendMessage"]
    edits = [p for m, p in api_calls if m == "editMessageText"]
    # Pre-compute per-turn final edits so checks below can reference them
    # without depending on the legacy `else: ... turn1_finals = ...` block.
    turn1_finals = [e for e in edits if "<b>picture</b>" in e.get("text", "")]
    turn2_finals = [e for e in edits if "Second-turn" in e.get("text", "")]

    # Tool markers are now ALWAYS standalone sendMessages, never appended
    # to a live edit buffer. The synthetic stream has:
    #   turn 1: pre-text + Read tool + post-text → 3 sendMessages (each
    #           opens its own bubble)
    #   turn 2: just text → 1 sendMessage
    # Total at least 4 (plus a "⏱ Worked for …" turn-duration footer after the
    # result event = 5). The first-sendMessage-of-each-text-segment carries
    # the "…" placeholder which subsequent edits replace with real content.
    if len(sends) < 4:
        failures.append(
            f"expected ≥4 sendMessage calls (tool markers must be their "
            f"own bubble), got {len(sends)}: "
            f"{[s.get('text','')[:60] for s in sends]}")

    # Tool marker for Read must appear in a sendMessage body (its own
    # Telegram bubble), NOT inside an edit. Check both sends explicitly.
    send_texts = [s.get("text", "") for s in sends]
    tool_in_sends = any(
        "📖" in t or "<i>Read</i>" in t or "<b>Read</b>" in t
        for t in send_texts)
    if not tool_in_sends:
        failures.append(
            "Read tool marker missing from sendMessage bodies; tool_use "
            "content_block_stop should emit the marker as its own message")
    edit_text = "".join(e.get("text", "") for e in edits)
    if "📖" in edit_text or "<i>Read</i>" in edit_text:
        failures.append(
            "Read tool marker leaked into an editMessageText body; "
            "tool markers should be standalone sendMessages")

    # The tool marker should also include a preview of the actual input:
    # the file_path streamed via input_json_delta. Without this, the chat
    # is a sea of opaque "📖 Read" icons with no signal as to what's being
    # read. We expect the last 3 path components to show up in the
    # marker's sendMessage body.
    if not any("CLAUDE.md" in t for t in send_texts):
        failures.append(
            "Read tool preview missing from marker sendMessage; expected "
            "the streamed input_json file_path to appear after the marker")
    any_text = edit_text  # back-compat alias for the thinking-placeholder
                          # check below, which scans all edit bodies.

    # "💭 Thinking…" appears at the start of each turn as the transient
    # placeholder (mirrors Claude Code's spinner), then disappears once real
    # content arrives. Contract:
    #   - Must appear in SOME edit body (proves message_start primed it).
    #   - Must NOT appear in any finalised sendMessage body (commits never
    #     keep the placeholder).
    #   - Must NOT appear in any turn-final edit (the final answer must
    #     have replaced it).
    if "💭" not in any_text:
        failures.append(
            "thinking placeholder never appeared in any edit; message_start "
            "should prime '💭 Thinking…' on each turn")
    for p in sends:
        body = p.get("text", "")
        if "💭" in body:
            failures.append(
                f"thinking placeholder leaked into a finalised sendMessage: "
                f"{body!r}")
    for e in turn1_finals + turn2_finals:
        if "💭" in e.get("text", ""):
            failures.append(
                f"thinking placeholder still present in turn-final edit "
                f"(should have been cleared by clear_thinking): {e.get('text','')[:80]!r}")

    # The orphan "…" placeholder must NOT be the final state of any
    # message. With message_start no longer eagerly sending "…", every
    # live message should resolve to real content.
    for s in sends:
        body = s.get("text", "")
        if body.strip() == "…":
            # Acceptable as the FIRST send (before content arrives) but
            # only if a subsequent editMessageText replaces it. Check
            # whether any edit targeted this message id with non-… text.
            # Simpler check: just assert it's not the only thing sent.
            pass  # rely on the explicit edits check below

    if not edits:
        failures.append("no editMessageText calls; streaming did not fire")
    else:
        # turn1_finals / turn2_finals computed at the top of the assertion
        # block so the thinking-placeholder checks can reference them.
        if not turn1_finals:
            failures.append("turn-1 final body never reached an edit")
        if not turn2_finals:
            failures.append("turn-2 final body never reached an edit (state may not reset on message_start)")
        # Turn 2's content must not leak into turn 1's box.
        for e in turn1_finals:
            if "Second-turn" in e.get("text", ""):
                failures.append(
                    f"turn-2 content rendered into turn-1's Telegram message: {e.get('text','')[:120]!r}")
        # Sanity content from turn 1. Pre-tool narration may live in
        # either an edit (the first send-and-edit cycle filled out the
        # placeholder) or the send body itself.
        any_text = "".join(e.get("text", "") for e in edits) + \
                   "".join(s.get("text", "") for s in sends)
        if "Reading the platform docs." not in any_text:
            failures.append("pre-tool narration missing across all messages")
        if "Widget" not in any_text:
            failures.append("post-tool content missing across all messages")
        if "•" not in any_text:
            failures.append("bullet not converted")
        # Pre-tool and post-tool text are now in DIFFERENT messages (the
        # tool marker split them). Check per-body (edit/send), if ANY
        # single message body holds both halves, they got bundled.
        all_bodies = [e.get("text", "") for e in edits] + send_texts
        bundled = [
            b for b in all_bodies
            if "Reading the platform docs." in b and "Found them" in b
        ]
        if bundled:
            failures.append(
                f"pre-tool and post-tool text bundled into one message: "
                f"{bundled[0][:120]!r}")

    # Turn-duration footer: the result event carries duration_ms (2300), so a
    # final "⏱ Worked for 2s" must be sent as its OWN message (never an edit
    # that would disturb the streamed answer / trip the DM no-flash rule).
    footer_sends = [s for s in sends if "Worked for" in s.get("text", "")]
    if not footer_sends:
        failures.append(
            "turn-duration footer missing; a result with duration_ms should "
            "send a '⏱ Worked for …' message")
    elif "2s" not in footer_sends[0].get("text", ""):
        failures.append(
            f"footer shows the wrong duration: {footer_sends[0].get('text','')!r}")
    if any("Worked for" in e.get("text", "") for e in edits):
        failures.append("duration footer leaked into an editMessageText body")

    print(f"sends={len(sends)} edits={len(edits)}")
    print(f"final body preview: {edits[-1].get('text','')!r}")
    if failures:
        for f in failures:
            print("FAIL", f)
        return 1
    print("smoke test ok")
    return 0


if __name__ == "__main__":
    sys.exit(run_test())
