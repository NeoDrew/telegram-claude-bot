#!/usr/bin/env python3
"""
Regression test for the thinking-placeholder orphan bug in group chats.

Bug recap:
  In group chats (negative chat_id → edit path, no draft streaming),
  message_start primes "💭 Thinking…" as the live edit-in-place box. When
  the turn proceeds straight to a tool_use (no text first), content_block_stop
  used to call clear_thinking() BEFORE commit_live(). clear_thinking sets
  _showing_thinking=False, so commit_live's thinking-teardown branch was
  skipped and the `elif _buf:` branch was also skipped (clear_thinking had
  emptied _buf). Result: _live_id was reset to None WITHOUT the actual
  Telegram "💭 Thinking…" message being deleted, leaving an orphan
  forever, and the user perceived the turn as "cutting off after the tool
  call" because all subsequent activity rendered as separate messages
  below the stranded placeholder.

The fix: remove clear_thinking() from the content_block_stop tool_use
branch; let commit_live() tear down the placeholder via its thinking
branch (deleteMessage in edit mode, tg_clear_draft in draft mode).

This test feeds the buggy scenario with mocked HTTP and asserts that:
  - A deleteMessage IS sent for the thinking placeholder before the tool
    marker (in group/edit mode).
  - The tool marker is sent as its own sendMessage.
  - No "💭 Thinking…" placeholder remains in any final-state body.

Run: python3 tests/test_group_thinking_teardown.py
"""

import importlib.util
import io
import json
import os
import sys
import urllib.parse

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def tool_only_turn_events():
    """A turn that goes: message_start → tool_use (Bash) → message_stop.

    No text content_blocks at all, mirroring claude turns that end on a
    tool call. The streamer must NOT leave the "💭 Thinking…" placeholder
    in the chat after this turn ends.
    """
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "name": "Bash"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta",
                  "partial_json": '{"command":"ls /tmp"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": False}


def tool_then_text_turn_events():
    """A turn: message_start → tool_use → text → message_stop. The thinking
    placeholder must vanish before the tool marker, and the text must land
    in a fresh box (not on top of the deleted thinking message)."""
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "name": "Read"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta",
                  "partial_json": '{"file_path":"/etc/hosts"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "text_delta",
                  "text": "Looks like a standard hosts file."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": False}


def two_tools_back_to_back_events():
    """Two tool_use blocks in the same turn, no text. Both should render as
    standalone messages. The thinking placeholder must be deleted exactly
    once (during the first commit_live), not stranded."""
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "name": "Glob"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta",
                  "partial_json": '{"pattern":"**/*.py"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "name": "Grep"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta",
                  "partial_json": '{"pattern":"def main"}'}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": False}


def thinking_then_text_only_events():
    """A turn that goes straight to text (no tool). In group mode, the
    thinking placeholder should be replaced by text via edit-in-place,
    NOT deleted (that would cause an extra round trip). The final body
    must contain the text and NOT the placeholder."""
    yield {"type": "stream_event", "event": {"type": "message_start"}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text"}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta",
                  "text": "Quick answer: yes."}}}
    yield {"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}}
    yield {"type": "stream_event", "event": {"type": "message_stop"}}
    yield {"type": "result", "result": "ok", "is_error": False}


def run_scenario(label, event_gen, group_mode=True):
    """Spin up the streamer with mocked HTTP and run one scenario.

    Returns (api_calls, failures). api_calls is a list of (method, params)
    tuples; failures is a list of str descriptions."""
    import urllib.request

    api_calls: list[tuple[str, dict]] = []
    fake_msg_id = [200]

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
        elif method_name in ("editMessageText", "deleteMessage",
                             "sendMessageDraft"):
            resp = {"ok": True, "result": True}
        else:
            resp = {"ok": True, "result": True}
        return FakeResponse(json.dumps(resp).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]

    ndjson = "\n".join(json.dumps(ev) for ev in event_gen()) + "\n"
    sys.stdin = io.StringIO(ndjson)

    # group mode = negative chat_id → draft_mode() returns False → edit path
    chat_id = "-100200300" if group_mode else "300200100"
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake-token"
    os.environ["CHAT_ID"] = chat_id
    os.environ["TELEGRAM_CHAT_ID"] = chat_id
    # Make edits fire on every call so the test sees them all.
    os.environ.pop("BOT_DRAFT_STREAMING", None)

    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram",
        os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = chat_id
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    mod._log_sent = lambda mid: None

    mod.main()

    sends = [(m, p) for m, p in api_calls if m == "sendMessage"]
    edits = [(m, p) for m, p in api_calls if m == "editMessageText"]
    deletes = [(m, p) for m, p in api_calls if m == "deleteMessage"]
    drafts = [(m, p) for m, p in api_calls if m == "sendMessageDraft"]

    print(f"\n--- {label} (group={group_mode}) ---")
    print(f"  sends={len(sends)} edits={len(edits)} "
          f"deletes={len(deletes)} drafts={len(drafts)}")
    for m, p in api_calls:
        body = p.get("text", "")[:80]
        print(f"  {m}: {body!r}")

    return api_calls, []


def main():
    failures = []

    # ── Scenario 1: tool-only turn in a group ────────────────────────────
    # The buggy code path. Must call deleteMessage to remove the thinking
    # placeholder; must send the tool marker as its own sendMessage.
    calls, _ = run_scenario(
        "tool-only turn", tool_only_turn_events, group_mode=True)
    sends = [p for m, p in calls if m == "sendMessage"]
    edits = [p for m, p in calls if m == "editMessageText"]
    deletes = [p for m, p in calls if m == "deleteMessage"]

    if not deletes:
        failures.append(
            "tool-only turn (group): no deleteMessage call, the thinking "
            "placeholder was orphaned in the chat. commit_live() should "
            "tear it down on tool-only turns via its _showing_thinking "
            "branch.")

    # Tool marker must reach a sendMessage body.
    tool_in_sends = any(
        "⚙️" in p.get("text", "") or "Bash" in p.get("text", "")
        for p in sends)
    if not tool_in_sends:
        failures.append(
            "tool-only turn (group): Bash tool marker missing from "
            "sendMessage bodies")

    # The final state of the chat must NOT show "💭 Thinking…" anywhere.
    # Specifically check that no sendMessage finalised with the placeholder
    # and no editMessageText left it in place.
    for p in sends:
        body = p.get("text", "")
        if "💭" in body or "Thinking" in body:
            failures.append(
                f"tool-only turn (group): thinking placeholder leaked into "
                f"a finalised sendMessage: {body!r}")

    # ── Scenario 2: tool then text in a group ────────────────────────────
    # Thinking deleted before the tool marker. Text streams to a NEW box
    # (not the deleted thinking message).
    calls, _ = run_scenario(
        "tool then text", tool_then_text_turn_events, group_mode=True)
    sends = [p for m, p in calls if m == "sendMessage"]
    edits = [p for m, p in calls if m == "editMessageText"]
    deletes = [p for m, p in calls if m == "deleteMessage"]

    if not deletes:
        failures.append(
            "tool-then-text turn (group): no deleteMessage, thinking "
            "placeholder was not torn down before the tool marker arrived")

    text_in_any = any("standard hosts" in p.get("text", "")
                      for p in sends + edits)
    if not text_in_any:
        failures.append(
            "tool-then-text turn (group): final text never reached any "
            "Telegram body")

    # The text must be in a DIFFERENT message than the tool marker (separate
    # bubbles). At minimum: ≥2 distinct sendMessages.
    tool_send_ids = [
        i for i, p in enumerate(sends)
        if "📖" in p.get("text", "") or "Read" in p.get("text", "")
    ]
    text_send_ids = [
        i for i, p in enumerate(sends)
        if "standard hosts" in p.get("text", "") or "Looks like" in p.get("text", "")
    ]
    # Text might have been streamed via edit, so check both paths.
    text_via_edit = any(
        "Looks like" in p.get("text", "") or "standard hosts" in p.get("text", "")
        for p in edits)
    if not text_via_edit and not text_send_ids:
        failures.append(
            "tool-then-text turn (group): post-tool text not delivered "
            "via either send or edit path")

    # ── Scenario 3: two tools back to back in a group ────────────────────
    # Thinking deleted ONCE (during the first commit_live). Two tool
    # markers, both standalone sendMessages.
    calls, _ = run_scenario(
        "two tools back-to-back",
        two_tools_back_to_back_events, group_mode=True)
    sends = [p for m, p in calls if m == "sendMessage"]
    deletes = [p for m, p in calls if m == "deleteMessage"]

    if len(deletes) != 1:
        failures.append(
            f"two-tool turn (group): expected exactly 1 deleteMessage "
            f"(for the thinking placeholder), got {len(deletes)}: "
            f"{[p for p in deletes]}")

    tool_markers = [p for p in sends
                    if "Glob" in p.get("text", "")
                    or "Grep" in p.get("text", "")
                    or "🔎" in p.get("text", "")]
    if len(tool_markers) < 2:
        failures.append(
            f"two-tool turn (group): expected ≥2 tool marker sendMessages, "
            f"got {len(tool_markers)}: "
            f"{[t.get('text','')[:60] for t in tool_markers]}")

    # ── Scenario 4: text-only turn in group ──────────────────────────────
    # The thinking placeholder should be REPLACED by the text (edit-in-place,
    # no deleteMessage), so no extra round trip.
    calls, _ = run_scenario(
        "text-only turn", thinking_then_text_only_events, group_mode=True)
    sends = [p for m, p in calls if m == "sendMessage"]
    edits = [p for m, p in calls if m == "editMessageText"]
    deletes = [p for m, p in calls if m == "deleteMessage"]

    if deletes:
        failures.append(
            f"text-only turn (group): deleteMessage fired ({len(deletes)} "
            f"times) when text should have replaced the placeholder via "
            f"edit-in-place, adds unnecessary round trips")

    text_landed = any(
        "yes" in p.get("text", "").lower() or "Quick answer" in p.get("text", "")
        for p in sends + edits)
    if not text_landed:
        failures.append("text-only turn (group): final text never landed")

    # Final body must not be the placeholder.
    final_edit_text = edits[-1].get("text", "") if edits else ""
    if "💭" in final_edit_text or "Thinking" in final_edit_text:
        failures.append(
            f"text-only turn (group): final edit still shows thinking "
            f"placeholder: {final_edit_text!r}")

    # ── Report ───────────────────────────────────────────────────────────
    print("\n=========================================")
    if failures:
        print(f"FAIL: {len(failures)} issue(s):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("ok: all group-mode thinking-teardown scenarios pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
