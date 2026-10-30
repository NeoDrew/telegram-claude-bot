#!/usr/bin/env python3
"""Tests for the MESSAGE_TOO_LONG recovery path in stream_to_telegram.py.

Telegram's editMessageText / sendMessage cap at 4096 chars of HTML output.
Heavily-formatted markdown can expand 1.5 to 3x (e.g. `code` → <code>code</code>,
pipe tables → padded <pre> blocks), so a buffer that's under the markdown
soft cap can still 400 with MESSAGE_TOO_LONG. Before this fix the streamer
treated those 400s as ordinary failures and kept retrying the same too-long
buffer until the turn ended, freezing the visible message at the last
successful edit and silently truncating the tail.

These tests pin the recovery contract:
  - When an edit returns 400 MESSAGE_TOO_LONG, the streamer splits the
    buffer at a safe html-length boundary and sends the tail as a new
    message rather than dropping it.
  - The proactive html-length check rotates before crossing the limit when
    the buffer expansion is predictable.
"""
import importlib.util
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class _FakeResp:
    def __init__(self, body):
        self._body = body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._body


def _too_long_error(url):
    body = json.dumps({
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: MESSAGE_TOO_LONG",
    }).encode()
    return urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))


def _load_streamer(chat_id, label):
    """Load stream_to_telegram as a fresh module so each test gets its own
    state. Returns the module with credentials patched in."""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    name = "s2t_" + str(label).replace("-", "n")
    spec = importlib.util.spec_from_file_location(
        name, SCRIPTS / "stream_to_telegram.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN = "fake-token"
    mod.TELEGRAM_CHAT_ID = str(chat_id)
    mod.API_URL = "https://api.telegram.org/botfake-token"
    mod.EDIT_INTERVAL = 0.0
    mod.GROUP_EDIT_INTERVAL = 0.0
    mod.DRAFT_INTERVAL = 0.0
    mod._log_sent = lambda mid: None
    return mod


class TestSafeSplit(unittest.TestCase):
    """Unit test for _safe_split: it must return a (head, tail) pair where
    head fits under the cap and head+tail reconstructs the input."""

    def setUp(self):
        self.mod = _load_streamer(123, "safe_split")

    def test_short_input_unchanged(self):
        head, tail = self.mod._safe_split("hello world", max_html=4000)
        self.assertEqual(head, "hello world")
        self.assertEqual(tail, "")

    def test_long_input_splits(self):
        # 6000 chars of plain text → no markdown expansion, so html_format
        # is ~6000 too. Cap at 1000 → head ≈ 1000 chars, tail = rest.
        buf = "a" * 6000
        head, tail = self.mod._safe_split(buf, max_html=1000)
        self.assertEqual(head + tail, buf)
        self.assertLessEqual(len(self.mod.html_format(head)), 1000)
        self.assertGreater(len(head), 0)
        self.assertGreater(len(tail), 0)

    def test_split_accounts_for_expansion(self):
        # A buffer that's only 1500 markdown chars but expands past 3000
        # html chars (every word wrapped in `code`). Cap at 2000 forces a
        # split despite the markdown being short.
        word = "`x` " * 400      # 4 markdown chars per word, ~17 html chars
        head, tail = self.mod._safe_split(word, max_html=2000)
        self.assertLessEqual(len(self.mod.html_format(head)), 2000)
        self.assertEqual(head + tail, word)

    def test_prefers_paragraph_boundary(self):
        # Buffer with clear paragraph breaks. The split should land on a
        # \n\n boundary rather than mid-sentence, so per-line markdown
        # (bold/italic/etc.) can't be cut in half.
        paragraphs = [
            "Para one is plain prose, **with some bold**, finishing here.",
            "Para two has _italic_ and `inline code` mid-sentence.",
            "Para three is also short prose.",
        ] * 50
        buf = "\n\n".join(paragraphs)
        head, tail = self.mod._safe_split(buf, max_html=600)
        self.assertEqual(head + tail, buf)
        # Head must end on a paragraph boundary (i.e. trailing "\n\n")
        # AND tail must start on a non-newline character (first char of
        # the next paragraph).
        self.assertTrue(
            head.endswith("\n\n"),
            f"split did not land on paragraph boundary; head ends with "
            f"{head[-10:]!r}")
        self.assertFalse(tail.startswith("\n"))

    def test_fenced_block_is_closed_and_reopened(self):
        # An unclosed fence in the head would render as literal backticks
        # on both sides. The repair adds a closer and opener so both
        # halves are syntactically complete code blocks.
        # Force the split to land INSIDE a fenced block: build a buffer
        # where the fence opens early, the safe-size point is inside it,
        # and the fence closes much later.
        prefix = "Intro paragraph.\n\n"
        fence_open = "```python\n"
        long_code = "x = 1\n" * 400      # 6 chars per line × 400 = 2400 chars
        fence_close = "```\n"
        suffix = "\n\nFinal paragraph."
        buf = prefix + fence_open + long_code + fence_close + suffix
        head, tail = self.mod._safe_split(buf, max_html=800)
        # Both halves now have a balanced ``` count, so each renders as
        # proper monospace.
        self.assertEqual(
            head.count("```") % 2, 0,
            f"head has unclosed ``` after repair: {head[-80:]!r}")
        self.assertEqual(
            tail.count("```") % 2, 0,
            f"tail has unclosed ``` after repair: {tail[:80]!r}")
        # Head still fits under the cap after appending the closer.
        self.assertLessEqual(len(self.mod.html_format(head)), 900)

    def test_no_paragraph_break_falls_through(self):
        # Pathological: one giant paragraph with no \n\n. We still split
        # at the raw safe index (no walk-back available) and must NOT
        # infinite-loop or return an empty head.
        buf = "x" * 6000
        head, tail = self.mod._safe_split(buf, max_html=1000)
        self.assertGreater(len(head), 0)
        self.assertGreater(len(tail), 0)
        self.assertEqual(head + tail, buf)


class TestEditModeRecovery(unittest.TestCase):
    """Group chats (negative chat_id) use the edit path. When edit 400s with
    MESSAGE_TOO_LONG the streamer must split the buffer and send the tail
    as a separate sendMessage."""

    def _run(self, edit_fail_first_n=1, fail_by_content=False):
        """Feed a synthetic stream where the final edit fails with
        MESSAGE_TOO_LONG. Returns the list of (method, params) calls
        observed.

        Two failure modes:
        - fail_by_content=False (default): fail the first `edit_fail_first_n`
          editMessageText calls, REGARDLESS of which message they target
          (the original design: every editMessageText counts, including
          the "💭 Thinking…" placeholder's own edit).
        - fail_by_content=True: fail editMessageText specifically when it's
          editing the long test payload (any number of times, no count
          cap), leaving unrelated edits (like the thinking placeholder)
          alone. This is what test_split_sends_tail_as_new_message uses;
          a count-based budget shared across UNRELATED edits made this
          test brittle to exactly how many editMessageText calls happen
          before the real payload's own recovery cycle (e.g. the
          background-dispatch change legitimately skips a
          redundant retry once a background job has already confirmed
          MESSAGE_TOO_LONG, shifting the count without changing the
          actual recovery guarantee this test exists to verify).
        """
        mod = _load_streamer(-987654, "edit_recovery")
        # Disable draft mode regardless of chat_id polarity (defensive).
        os.environ["BOT_DRAFT_STREAMING"] = "0"
        calls = []
        fail_remaining = [edit_fail_first_n]
        msg_id = [500]

        import urllib.request

        def fake_urlopen(req, timeout=None):
            method = req.full_url.rsplit("/", 1)[-1]
            params = dict(urllib.parse.parse_qsl(
                req.data.decode() if req.data else ""))
            calls.append((method, params))
            if method == "editMessageText":
                if fail_by_content:
                    if "x" * 100 in params.get("text", ""):
                        raise _too_long_error(req.full_url)
                elif fail_remaining[0] > 0:
                    fail_remaining[0] -= 1
                    raise _too_long_error(req.full_url)
            if method == "sendMessage":
                msg_id[0] += 1
                return _FakeResp(json.dumps({
                    "ok": True,
                    "result": {"message_id": msg_id[0]},
                }).encode())
            return _FakeResp(json.dumps({"ok": True, "result": True}).encode())

        urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]

        # Synthetic stream: message_start, one long text block, message_stop.
        # Build enough text to push past 2000 markdown chars so the proactive
        # html check engages; but we'll still force the reactive path by
        # making the final edit 400 with MESSAGE_TOO_LONG.
        chunk = "x" * 3500   # fits in the markdown cap, won't proactively rotate
        events = [
            {"type": "stream_event", "event": {"type": "message_start"}},
            {"type": "stream_event", "event": {
                "type": "content_block_start",
                "content_block": {"type": "text"}}},
            {"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": chunk}}},
            {"type": "stream_event", "event": {
                "type": "content_block_stop"}},
            {"type": "stream_event", "event": {"type": "message_stop"}},
            {"type": "result", "result": "ok", "is_error": False},
        ]
        sys.stdin = io.StringIO(
            "\n".join(json.dumps(e) for e in events) + "\n")
        mod.main()
        return calls

    def test_split_sends_tail_as_new_message(self):
        calls = self._run(fail_by_content=True)
        sends = [p for m, p in calls if m == "sendMessage"]
        edits = [p for m, p in calls if m == "editMessageText"]
        # Two outputs: the initial "…" placeholder + recovery from the
        # MESSAGE_TOO_LONG cascade (head as edit retry, tail as new send,
        # plus the live placeholder). Just assert at LEAST one sendMessage
        # carried real content.
        bodies = " ".join(s.get("text", "") for s in sends)
        edit_bodies = " ".join(e.get("text", "") for e in edits)
        combined = bodies + edit_bodies
        # The 3500 "x" chars must end up SOMEWHERE in the calls, split or
        # not. Without recovery, the buffer would just sit and 400 forever.
        self.assertIn("x" * 100, combined,
                      "stream content never reached Telegram after MESSAGE_TOO_LONG")
        # There must be at least ONE successful sendMessage carrying content
        # (the tail split-off). Without recovery, sends == [initial "…"].
        non_placeholder_sends = [s for s in sends if s.get("text", "") != "…"]
        self.assertGreater(
            len(non_placeholder_sends), 0,
            "no recovery sendMessage was issued after MESSAGE_TOO_LONG")


class TestCommitFallbackOnGenericEditFailure(unittest.TestCase):
    """When the final commit_live edit fails for a NON-too-long reason
    (rate-limit cooldown, transient network, residual parse 400), the
    streamer must fall back to sendMessage so the user sees the content
    instead of a stuck "…" placeholder. The 429 scenario is the load-
    bearing case: a 30s+ Telegram cooldown used to swallow every edit
    attempt INCLUDING the final flush, leaving a group room stuck on "…".
    """

    def test_send_fallback_when_edit_fails(self):
        """Edit returns None (mock simulates a generic transient failure
        with no MESSAGE_TOO_LONG signal). commit_live should retry, then
        fall back to a fresh sendMessage carrying the buffer content."""
        mod = _load_streamer(-555000, "fallback")
        os.environ["BOT_DRAFT_STREAMING"] = "0"

        # Stub _wait_for_cooldown to a no-op so the test doesn't sleep.
        mod._wait_for_cooldown = lambda reason="": None

        calls = []
        msg_id = [700]
        # Every editMessageText fails with a generic 400 (no MESSAGE_TOO_LONG
        # description). sendMessage succeeds. tg_api returns None for the
        # edit (the failure is logged but not classified as too_long), then
        # commit_live falls back to send.
        import urllib.request

        def fake_urlopen(req, timeout=None):
            method = req.full_url.rsplit("/", 1)[-1]
            params = dict(urllib.parse.parse_qsl(
                req.data.decode() if req.data else ""))
            calls.append((method, params))
            if method == "editMessageText":
                body = json.dumps({
                    "ok": False, "error_code": 400,
                    "description": "Bad Request: some transient error",
                }).encode()
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
            if method == "sendMessage":
                msg_id[0] += 1
                return _FakeResp(json.dumps({
                    "ok": True, "result": {"message_id": msg_id[0]},
                }).encode())
            return _FakeResp(json.dumps({"ok": True, "result": True}).encode())

        urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]

        events = [
            {"type": "stream_event", "event": {"type": "message_start"}},
            {"type": "stream_event", "event": {
                "type": "content_block_start",
                "content_block": {"type": "text"}}},
            {"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta",
                          "text": "Here is the real reply that must reach you."}}},
            {"type": "stream_event", "event": {"type": "content_block_stop"}},
            {"type": "stream_event", "event": {"type": "message_stop"}},
        ]
        sys.stdin = io.StringIO(
            "\n".join(json.dumps(e) for e in events) + "\n")
        mod.main()

        sends = [p for m, p in calls if m == "sendMessage"]
        # The initial "…" placeholder + the fallback send carrying real
        # content. Without the fallback, we'd only see the placeholder
        # and the user would be stuck on "…".
        real_sends = [
            s for s in sends
            if "Here is the real reply" in s.get("text", "")
        ]
        self.assertGreater(
            len(real_sends), 0,
            f"no sendMessage carried the real reply; got sends={sends!r}")


class TestWaitForCooldown(unittest.TestCase):
    """_wait_for_cooldown must actually sleep when rate-limited (so the
    final flush happens AFTER the cooldown) and must NOT sleep when not."""

    def setUp(self):
        self.mod = _load_streamer(123, "cooldown_wait")

    def test_no_sleep_when_not_rate_limited(self):
        slept = []
        self.mod.time.sleep = lambda s: slept.append(s)  # type: ignore[assignment]
        self.mod._rate_limited_until = 0.0
        self.mod._wait_for_cooldown("test")
        self.assertEqual(slept, [], "slept while not rate-limited")

    def test_sleeps_until_cooldown_expires(self):
        slept = []
        self.mod.time.sleep = lambda s: slept.append(s)  # type: ignore[assignment]
        # Pretend we got rate-limited 5s into the future.
        self.mod._rate_limited_until = self.mod.time.time() + 5
        self.mod._wait_for_cooldown("test")
        self.assertEqual(len(slept), 1, "wait_for_cooldown should sleep once")
        self.assertGreater(
            slept[0], 4.0,
            f"slept too short: {slept[0]} (expected ~5s + 0.5s buffer)")
        self.assertLessEqual(
            slept[0], self.mod.COOLDOWN_WAIT_MAX,
            f"slept past the safety cap: {slept[0]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
