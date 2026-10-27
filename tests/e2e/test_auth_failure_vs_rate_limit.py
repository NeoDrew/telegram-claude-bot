#!/usr/bin/env python3
"""
E2E: an auth failure (expired/invalid OAuth token) is a DIFFERENT failure
mode from a rate limit and must be handled distinctly: stream_to_telegram.
py's check_auth_failure() path, not _park_room_for_rate_limit(). Conflating
them would be a real bug: an auth failure parked as a "rate limit" would
queue forever waiting for a reset time that will never come (a stale token
needs a human to fix it, not a clock), while a rate limit painted as "go
run /login" sends the user chasing a problem that doesn't exist.

Two cases, deliberately different SHAPES of what claude actually emits:

  1. Plain streamed text containing the error phrase. This is the
     DOCUMENTED real shape per check_auth_failure's own docstring:
     "Claude reports auth errors as plain assistant text... rather than
     as a structured is_error event". Expect exactly ONE clean alert.
  2. A structured is_error result with api_error_status="401" (what a
     more conventional API-error envelope would look like, if claude
     ever emits one this way for auth problems specifically). This used
     to produce THREE separate messages for one failure (the raw
     result-text fallback, a generic "claude error: 401", and the proper
     alert); see the is_error branch's own
     comment in stream_to_telegram.py. Now asserted at exactly one
     message, same as the documented shape, since asserting an exact
     count catches sloppy "eh, some auth-ish message went out"
     test-writing that would miss a real duplicate-message defect.

Run: python3 tests/e2e/test_auth_failure_vs_rate_limit.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, write_fake_claude_script, DM_CHAT_ID  # noqa: E402


def is_auth_shaped(text):
    low = text.lower()
    return ("invalid authentication credentials" in low
            or "failed to authenticate" in low
            or "invalid api key" in low
            or "oauth token expired" in low)


def case_plain_text_shape():
    """The documented real shape: auth failure as ordinary streamed text."""
    h = E2EHarness(label="auth-failure-text-shape")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"text": "Failed to authenticate. API Error: 401 Invalid "
                  "authentication credentials"},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()
        h.inject_dm("do something")

        h.wait_for_call("sendMessage", lambda p: is_auth_shaped(p.get("text", "")),
                         timeout=20)
        import time
        time.sleep(1.5)  # let any (buggy) extra alerts land before counting

        auth_alerts = [c for c in h.all_calls("sendMessage")
                        if is_auth_shaped(c["params"].get("text", ""))]
        print(f"  {len(auth_alerts)} auth-shaped message(s): "
              f"{[c['params'].get('text', '')[:80] for c in auth_alerts]}")
        assert len(auth_alerts) == 1, (
            f"expected exactly ONE clean auth alert for the documented "
            f"plain-text shape, got {len(auth_alerts)} (spammy/redundant "
            f"alerting)")
        print("ok: plain-text auth failure produces exactly one alert")

        rate_limit_texts = [c for c in h.all_calls("sendMessage")
                             if "Rate limited" in c["params"].get("text", "")]
        assert not rate_limit_texts, (
            f"must not be misclassified as a rate limit: {rate_limit_texts}")
        print("ok: not misclassified as a rate limit")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()
        os.unlink(script_path)


def case_structured_is_error_shape():
    """A structured is_error result with api_error_status set, distinct
    from the documented plain-text shape. Used to produce three messages
    for one failure (found via this test); now fixed and asserted at
    exactly one, same standard as the documented shape."""
    h = E2EHarness(label="auth-failure-structured-shape")
    fd, script_path = tempfile.mkstemp(suffix=".json", prefix="fake-claude-script-")
    os.close(fd)
    write_fake_claude_script(script_path, [
        {"auth_failure": {"text": "Invalid API key. Please run /login.",
                           "api_error_status": "401"}},
    ])
    try:
        h.setup(fake_claude_script=script_path,
                extra_env={"BOT_DRAFT_STREAMING": "0"})
        h.start_listener()
        h.inject_dm("do something")

        h.wait_for_call("sendMessage", lambda p: is_auth_shaped(p.get("text", "")),
                         timeout=20)
        import time
        time.sleep(1.5)  # let any (regressed) extra alerts land before counting

        all_sends = h.all_calls("sendMessage")
        print(f"  {len(all_sends)} total sendMessage call(s) for this turn:")
        for c in all_sends:
            print(f"    {c['params'].get('text', '')[:100]!r}")
        assert len(all_sends) == 1, (
            f"expected exactly ONE message for a structured is_error auth "
            f"failure, got {len(all_sends)} (the historical bug: a raw "
            f"result-text dump + a generic 'claude error: 401' + the "
            f"proper alert, three sends for one failure)")
        assert is_auth_shaped(all_sends[0]["params"].get("text", "")), (
            f"the single message sent wasn't the proper auth alert: "
            f"{all_sends[0]['params']}")
        print("ok: structured is_error auth failure produces exactly one, "
              "correct alert")
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        return False
    finally:
        h.teardown()
        os.unlink(script_path)


def main():
    results = {
        "plain_text_shape (documented)": case_plain_text_shape(),
        "structured_is_error_shape": case_structured_is_error_shape(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
