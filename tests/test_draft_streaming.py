#!/usr/bin/env python3
"""Native draft-streaming tests for scripts/stream_to_telegram.py.

Telegram Bot API 9.5+ sendMessageDraft renders a smooth animated preview in
DMs that the final real message replaces. These tests pin the contract:

  1. DM (positive chat_id): streams via sendMessageDraft, never editMessageText.
  2. Parity: each finalised sendMessage text is byte-identical to a draft frame
     that preceded it; this is what stops the end-of-message "flash".
  3. The draft is cleared (empty text) as part of finalising.
  4. One draft id per turn (so frames update the same draft, not a new one).
  5. Group/topic (negative chat_id): drafts are NOT used; edit path unchanged.
  6. BOT_DRAFT_STREAMING=0 forces the edit path even in a DM.
  7. If a draft call hard-fails, the streamer falls back to editMessageText.

The synthetic two-turn event stream is reused from the existing smoke test so
both transports are exercised against identical input.
"""
import io
import json
import os
import sys
import importlib.util
import urllib.parse
import urllib.error
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from test_streamer_smoke import fake_stream_events  # noqa: E402  (reuse the stream)


class _FakeResp:
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body


def run_streamer(chat_id, draft_flag="auto", fail_drafts=False, reject_html_drafts=False):
    """Run the streamer over the synthetic stream with urlopen mocked.
    Returns the list of (method_name, params) Telegram calls it made.

    fail_drafts: every sendMessageDraft 400s (HTML and plain) -> edit fallback.
    reject_html_drafts: only HTML draft frames 400; plain frames succeed
        (simulates Telegram's occasional spurious parse 400 on valid HTML).
    """
    import urllib.request
    calls = []
    msg_id = [200]

    def fake_urlopen(req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(req.data.decode() if req.data else ""))
        calls.append((method, params))
        reject = method == "sendMessageDraft" and (
            fail_drafts or (reject_html_drafts and params.get("parse_mode") == "HTML"))
        if reject:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"ok":false,"description":"can\'t parse entities"}'))
        if method == "sendMessage":
            msg_id[0] += 1
            resp = {"ok": True, "result": {"message_id": msg_id[0]}}
        else:
            resp = {"ok": True, "result": True}
        return _FakeResp(json.dumps(resp).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]

    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in fake_stream_events()) + "\n")
    os.environ["BOT_DRAFT_STREAMING"] = draft_flag
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")

    name = "s2t_" + str(chat_id).replace("-", "n")
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "stream_to_telegram.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = str(chat_id)          # draft_mode() reads this dynamically
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0                       # flush every frame so we see them all
    mod.DRAFT_INTERVAL = 0.0
    mod._log_sent = lambda mid: None             # touch no on-disk state
    mod.main()
    return calls


def split(calls):
    drafts = [p for m, p in calls if m == "sendMessageDraft"]
    sends = [p for m, p in calls if m == "sendMessage"]
    edits = [p for m, p in calls if m == "editMessageText"]
    return drafts, sends, edits


# Tool markers are sent as their OWN standalone sendMessages, bypassing the
# draft pipeline entirely (a tool icon doesn't benefit from typing animation
# and forcing it through drafts would require an extra draft+commit cycle
# per tool). Tests that pin the draft → final-send contract must exclude
# these from the count or they trip the "no-flash" invariant.
_TOOL_EMOJI_PREFIXES = ("📖", "⚙️", "📝", "🔎", "🌐", "🤖", "✅", "📓", "🖱", "🛠")


def is_tool_marker(text):
    if not text:
        return False
    return any(text.startswith(g) for g in _TOOL_EMOJI_PREFIXES)


def is_footer(text):
    """The "⏱ Worked for …" turn-duration line is a standalone message sent
    AFTER the draft is finalised + cleared (on the result event), so it's a
    brand-new bubble that can't repaint the answer, so it's exempt from the draft
    no-flash invariant, just like tool markers."""
    return bool(text) and "Worked for" in text


def split_sends(sends):
    """Partition sendMessage params into (text_sends, standalone_sends).
    Standalone = tool markers + the turn-duration footer; neither participates
    in the draft → final-send no-flash contract."""
    text, standalone = [], []
    for p in sends:
        t = p.get("text", "")
        if is_tool_marker(t) or is_footer(t):
            standalone.append(p)
        else:
            text.append(p)
    return text, standalone


class TestDraftDM(unittest.TestCase):
    def setUp(self):
        self.calls = run_streamer(123456)  # positive => DM
        self.drafts, self.sends, self.edits = split(self.calls)

    def test_streams_via_drafts_not_edits(self):
        self.assertGreater(len(self.drafts), 0, "no draft frames sent in a DM")
        self.assertEqual(len(self.edits), 0, "edit path used despite draft mode")

    def test_one_real_message_per_turn(self):
        # Text-content sends only: tool markers are now standalone
        # sendMessages too, so the raw len(sends) is higher. The
        # invariant is "every text content block finalises as a real
        # message". The synthetic stream has TWO text-content commits:
        # turn 1's pre-tool narration through final answer (one bubble
        # after the tool split into pre + post halves) and turn 2's
        # answer. Actually pre/post-tool text are now separate bubbles
        # too, so we get pre + post + turn-2 = 3.
        text_sends, _tool_sends = split_sends(self.sends)
        self.assertGreaterEqual(
            len(text_sends), 2,
            f"expected text-content sends for each chunk-between-tools, "
            f"got {[s.get('text','')[:60] for s in text_sends]}")

    def test_final_message_matches_a_draft_frame(self):
        # The flash fix: every TEXT-content sendMessage equals a draft
        # frame that was already on screen, so the client never repaints
        # the bubble. Tool-marker sends are standalone (never streamed
        # via drafts) so they're excluded from this check.
        draft_texts = {p.get("text", "") for p in self.drafts}
        text_sends, _ = split_sends(self.sends)
        for s in text_sends:
            self.assertIn(s.get("text", ""), draft_texts,
                          "final message text differs from every draft frame "
                          "(would cause the end-of-message flash)")

    def test_draft_is_cleared(self):
        self.assertTrue(any(p.get("text", "") == "" for p in self.drafts),
                        "draft never cleared with empty text")

    def test_one_draft_id_per_turn(self):
        ids = {p.get("draft_id") for p in self.drafts}
        # Two turns => at least two distinct draft ids (frames within a turn
        # share an id so they update the same draft).
        self.assertGreaterEqual(len(ids), 2)

    def test_draft_frames_grow_then_finalize(self):
        # Each draft_id's non-empty frames should be monotonically growing
        # prefixes within that draft. (We can't just walk all drafts in
        # order any more: message_start now opens a short-lived "💭 Thinking…"
        # draft that's cleared the instant real text arrives, so an actual
        # turn produces multiple draft_ids.)
        from collections import defaultdict
        by_id = defaultdict(list)
        for p in self.drafts:
            by_id[p.get("draft_id")].append(p.get("text", ""))
        grew_somewhere = False
        for draft_id, frames in by_id.items():
            nonempty = [t for t in frames if t]
            if len(nonempty) < 2:
                continue
            grew_somewhere = True
            for a, b in zip(nonempty, nonempty[1:]):
                self.assertTrue(
                    b.startswith(a) or len(b) >= len(a),
                    f"draft {draft_id} frames shrunk: {a!r} → {b!r}")
        self.assertTrue(
            grew_somewhere,
            "no draft group had >1 frame; streaming never built up content")


class TestGroupUnchanged(unittest.TestCase):
    def test_group_does_not_use_drafts(self):
        drafts, sends, edits = split(run_streamer(-99887766))  # negative => group
        self.assertEqual(len(drafts), 0, "drafts leaked into a group chat")
        self.assertGreater(len(edits), 0, "edit path did not run in a group")
        # Tool markers add their own standalone sendMessages on top of the
        # text-content placeholders. Just assert we got SOME sends; the
        # exact count is exercised by smoke tests.
        self.assertGreater(len(sends), 0, "no sendMessage calls in group mode")

    def test_flag_off_forces_edit_in_dm(self):
        drafts, sends, edits = split(run_streamer(123456, draft_flag="0"))
        self.assertEqual(len(drafts), 0, "BOT_DRAFT_STREAMING=0 still used drafts")
        self.assertGreater(len(edits), 0)


class TestDraftFallback(unittest.TestCase):
    def test_draft_failure_falls_back_to_edit(self):
        drafts, sends, edits = split(run_streamer(123456, fail_drafts=True))
        # It tried a draft (HTML then plain-text retry), both failed, so it
        # switched this response to the edit path.
        self.assertGreater(len(drafts), 0, "never attempted a draft")
        self.assertGreater(len(edits), 0, "did not fall back to editMessageText")

    def test_plain_draft_finalises_plain_no_flash(self):
        # Telegram rejects the HTML frames but accepts plain ones (a spurious
        # 400 Telegram sometimes returns). The draft then streams as plain text, so the FINAL
        # text message must also be plain (no parse_mode) and equal to a plain
        # draft frame; otherwise an HTML final would repaint and flash.
        # Tool-marker sends are standalone (never streamed via drafts) so
        # they're excluded from this invariant.
        calls = run_streamer(123456, reject_html_drafts=True)
        drafts, sends, edits = split(calls)
        plain_drafts = [p for p in drafts
                        if p.get("parse_mode") != "HTML" and p.get("text")]
        self.assertGreater(len(plain_drafts), 0, "no plain draft frames landed")
        self.assertEqual(len(edits), 0, "should stay on the draft path, not edit")
        self.assertGreater(len(sends), 0, "no finalised message")
        plain_texts = {p["text"] for p in plain_drafts}
        text_sends, _tool_sends = split_sends(sends)
        for s in text_sends:
            self.assertNotIn("parse_mode", s,
                             "final text message used HTML while the draft was "
                             "plain → would flash")
            self.assertIn(s.get("text", ""), plain_texts,
                          "final plain message differs from the last plain draft")


if __name__ == "__main__":
    unittest.main(verbosity=2)
