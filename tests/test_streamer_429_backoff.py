#!/usr/bin/env python3
"""
test_streamer_429_backoff.py - regression test for the adaptive 429 backoff
in scripts/stream_to_telegram.py.

Regression test: a single chat's long-running turn used to trip
Telegram's own per-message edit flood limit repeatedly, many 429s within
one room. The streamer already handled each individual 429
correctly (wait out retry_after, don't lose content), but resumed editing
at the exact same EDIT_INTERVAL/DRAFT_INTERVAL afterward every time, so a
long turn just re-tripped the same ceiling a few minutes later, over and
over, forever.

Fix: each 429 bumps a backoff level that widens maybe_flush's effective
interval; a sustained streak of clean calls steps it back down by one, so
a room that settles down earns its way back to full responsiveness rather
than degrading permanently after one old storm, or (the failure mode this
test targets) immediately re-tripping the same limit at full speed.

Tests tg_api()/_rl_backoff_multiplier() directly (not the full streaming
pipeline; test_streamer_smoke.py already covers that) with a fake
urlopen that can be told to fail with 429 or succeed on demand.

Run: python3 tests/test_streamer_429_backoff.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def load_module(fail_next, repo_dir):
    """fail_next: mutable list; pop a truthy value to make the next call
    raise a 429 HTTPError with retry_after=1, otherwise succeed."""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "555000111")
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.stdin = io.StringIO("")
    spec.loader.exec_module(mod)
    # Redirect REPO into a sandbox: tg_api()'s success path calls _log_sent(),
    # which writes state/rooms/<chat>/sent_msgs.log using REPO computed from
    # this module's own file location. Without this, every test run wrote a
    # real sent_msgs.log into the real repo's state/rooms/555000111/.
    mod.REPO = __import__("pathlib").Path(repo_dir)

    def fake_urlopen(req, timeout=None):
        if fail_next and fail_next[0]:
            fail_next[0] -= 1
            body = json.dumps({
                "ok": False, "error_code": 429,
                "parameters": {"retry_after": 1},
            }).encode()
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, io.BytesIO(body))
        return FakeResponse(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())

    urllib.request.urlopen = fake_urlopen
    return mod


def main():
    failures = []
    tmp = tempfile.mkdtemp()

    # Case 1: repeated 429s ratchet the backoff level up, capped at MAX.
    fail_next = [0]
    mod = load_module(fail_next, tmp)
    assert mod._rl_backoff_level == 0, "starts at 0"
    for i in range(mod._RL_BACKOFF_MAX + 2):
        fail_next[0] = 1
        mod._rate_limited_until = 0.0  # bypass the cooldown gate between calls
        mod.tg_api("editMessageText", {"chat_id": "x", "message_id": "1", "text": "y"})
    if mod._rl_backoff_level != mod._RL_BACKOFF_MAX:
        failures.append(
            f"case 1: expected backoff level capped at {mod._RL_BACKOFF_MAX} "
            f"after repeated 429s, got {mod._rl_backoff_level}")
    else:
        print(f"ok: repeated 429s ratchet backoff up to the cap "
              f"({mod._rl_backoff_level}, multiplier={mod._rl_backoff_multiplier():.2g}x)")

    # Case 2: the multiplier actually widens maybe_flush's effective
    # interval: the whole point of the fix. At max backoff it must be
    # meaningfully wider than baseline (the exact symptom: same
    # interval every retry, re-tripping the same limit).
    base = mod.EDIT_INTERVAL
    widened = base * mod._rl_backoff_multiplier()
    if not (widened > base * 2):
        failures.append(
            f"case 2: expected the widened interval to be at least 2x "
            f"baseline at max backoff, got base={base} widened={widened}")
    else:
        print(f"ok: interval widens from {base}s to {widened:.2f}s at max backoff")

    # Case 3: a sustained clean streak steps the level back down, one at a
    # time, not an instant snap back to 0 on the first success (that would
    # just re-trip the limit immediately, defeating the point).
    fail_next[0] = 0
    mod._rate_limited_until = 0.0
    for _ in range(mod._RL_DECAY_STREAK - 1):
        mod.tg_api("editMessageText", {"chat_id": "x", "message_id": "1", "text": "y"})
    if mod._rl_backoff_level != mod._RL_BACKOFF_MAX:
        failures.append(
            f"case 3a: backoff must NOT step down before a full clean "
            f"streak completes, got level={mod._rl_backoff_level} after "
            f"{mod._RL_DECAY_STREAK - 1}/{mod._RL_DECAY_STREAK} clean calls")
    else:
        print(f"ok: backoff holds steady through a partial clean streak "
              f"({mod._RL_DECAY_STREAK - 1}/{mod._RL_DECAY_STREAK})")
    mod.tg_api("editMessageText", {"chat_id": "x", "message_id": "1", "text": "y"})
    if mod._rl_backoff_level != mod._RL_BACKOFF_MAX - 1:
        failures.append(
            f"case 3b: expected exactly one step down after completing a "
            f"clean streak, got level={mod._rl_backoff_level} "
            f"(was {mod._RL_BACKOFF_MAX})")
    else:
        print(f"ok: backoff steps down by exactly one after a full clean streak "
              f"({mod._RL_BACKOFF_MAX} -> {mod._rl_backoff_level})")

    # Case 4: a fresh 429 resets the clean streak, so a call right at the
    # decay threshold doesn't count if a 429 landed in between.
    mod._rl_backoff_level = 1
    mod._rl_clean_streak = mod._RL_DECAY_STREAK - 1
    fail_next[0] = 1
    mod._rate_limited_until = 0.0
    mod.tg_api("editMessageText", {"chat_id": "x", "message_id": "1", "text": "y"})
    if mod._rl_clean_streak != 0:
        failures.append(
            f"case 4: a 429 must reset the clean streak to 0, got "
            f"{mod._rl_clean_streak}")
    elif mod._rl_backoff_level != 2:
        failures.append(
            f"case 4: the same 429 must also bump the level up by one, "
            f"got {mod._rl_backoff_level} (expected 2)")
    else:
        print("ok: a 429 resets the clean streak AND bumps the level, "
              "even mid-decay")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: adaptive 429 backoff ratchets up on repeated hits and "
          "decays gradually on sustained clean calls, not instantly")


if __name__ == "__main__":
    main()
