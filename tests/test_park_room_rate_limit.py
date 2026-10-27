#!/usr/bin/env python3
"""
test_park_room_rate_limit.py - regression test for the "_park_room_for_
rate_limit always writes now+300, ignoring the real computed reset time"
bug (found via tests/e2e/test_multi_room_restart_survival.py and
test_rate_limit_recovery.py flaking under load).

scripts/stream_to_telegram.py's _park_room_for_rate_limit() computed
`best_resets` from the cached rate_limit_event snapshot, then discarded it:
the final write always hardcoded `now_ts + 300` (5 minutes) regardless of
what best_resets said. Since this write lands BEFORE spawn_claude.sh's own
more accurate post-exit calculation, and spawn_claude.sh's idempotency
guard treats an existing .rate_limited_until as authoritative when it
looks "already parked", a real multi-hour rate limit got written as a
5-minute park. The room then retried in 5 minutes, hit the SAME real
limit again, wrote ANOTHER 5-minute park, and repeated, producing a
rate-limit message every 5 minutes.

Fixed by writing best_resets instead of the bare now_ts + 300 fallback
(the fallback is still used when best_resets is genuinely unknown, i.e.
the usage cache has no fresh blocked entry).

Run: python3 tests/test_park_room_rate_limit.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import time

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def load_module(repo_dir, chat_id):
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Redirect module-level path globals into the sandbox: functions that
    # reference REPO/USAGE_CACHE/TELEGRAM_CHAT_ID look them up on the
    # module at CALL time, so patching them post-import is sufficient and
    # doesn't require touching real production state.
    mod.REPO = __import__("pathlib").Path(repo_dir)
    mod.USAGE_CACHE = mod.REPO / "state" / "usage_cache.json"
    mod.TELEGRAM_CHAT_ID = chat_id
    return mod


def main():
    failures = []

    # Case 1: a real multi-hour limit in the usage cache must be used
    # verbatim, not discarded in favour of the bare 5-minute fallback.
    with tempfile.TemporaryDirectory() as tmp:
        chat_id = "555000111"
        mod = load_module(tmp, chat_id)
        (mod.REPO / "state" / "rooms" / chat_id).mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        real_resets_at = now + 5 * 3600  # a real 5-hour limit
        cache_path = mod.REPO / "state" / "usage_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "five_hour": {
                "utilization": 1.0,
                "status": "rejected",
                "resetsAt": real_resets_at,
                "isUsingOverage": False,
                "updatedAt": now,
            }
        }))

        mod._park_room_for_rate_limit()

        marker = mod.REPO / "state" / "rooms" / chat_id / ".rate_limited_until"
        if not marker.exists():
            failures.append("case 1: .rate_limited_until was not written at all")
        else:
            written = int(marker.read_text().strip())
            if written != real_resets_at:
                failures.append(
                    f"case 1: expected the REAL 5-hour resetsAt "
                    f"({real_resets_at}) to be written, got {written} "
                    f"(diff={written - real_resets_at}s). If this is "
                    f"close to now+300, the bare fallback is being used "
                    f"instead of the cached real value again.")
            else:
                print(f"ok: real multi-hour resetsAt used verbatim "
                      f"({written}, {(written - now) / 3600:.1f}h from now)")

    # Case 2: no usable cache entry at all falls back to the conservative
    # 5-minute guess (this path is still correct and expected).
    with tempfile.TemporaryDirectory() as tmp:
        chat_id = "555000111"
        mod = load_module(tmp, chat_id)
        (mod.REPO / "state" / "rooms" / chat_id).mkdir(parents=True, exist_ok=True)
        now = int(time.time())

        mod._park_room_for_rate_limit()

        marker = mod.REPO / "state" / "rooms" / chat_id / ".rate_limited_until"
        if not marker.exists():
            failures.append("case 2: .rate_limited_until was not written at all")
        else:
            written = int(marker.read_text().strip())
            if not (now + 250 <= written <= now + 350):
                failures.append(
                    f"case 2: expected the ~300s conservative fallback "
                    f"when no cache entry exists, got {written} "
                    f"(now={now}, diff={written - now}s)")
            else:
                print(f"ok: falls back to the ~300s guess when no cache "
                      f"entry exists ({written - now}s)")

    # Case 3: a SHORT real window (under 5 minutes) must also be used
    # verbatim, not silently upgraded to the longer bare fallback. This is
    # the exact case that surfaced the bug via the E2E suite's short test
    # fixtures (10s rate-limit windows).
    with tempfile.TemporaryDirectory() as tmp:
        chat_id = "555000111"
        mod = load_module(tmp, chat_id)
        (mod.REPO / "state" / "rooms" / chat_id).mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        short_resets_at = now + 10
        cache_path = mod.REPO / "state" / "usage_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "five_hour": {
                "utilization": 1.0,
                "status": "rejected",
                "resetsAt": short_resets_at,
                "isUsingOverage": False,
                "updatedAt": now,
            }
        }))

        mod._park_room_for_rate_limit()

        marker = mod.REPO / "state" / "rooms" / chat_id / ".rate_limited_until"
        written = int(marker.read_text().strip()) if marker.exists() else None
        if written != short_resets_at:
            failures.append(
                f"case 3: expected the SHORT real resetsAt ({short_resets_at}) "
                f"to be used, got {written} (a longer bare fallback winning "
                f"over a real short window is exactly the historical bug)")
        else:
            print(f"ok: a short real window (10s) is used verbatim, not "
                  f"pessimistically upgraded to the 300s fallback")

    # Case 4 (a rate-limit retry loop that spammed the chat): a WEEKLY limit resets more than 6h away.
    # The old 6h sanity bound discarded it and parked for 5 minutes, so every
    # room retried into the same limit every 5 minutes, around the clock.
    # Both 15h and 3 days away must be used verbatim.
    for label, ahead in (("15h", 15 * 3600), ("3 days", 3 * 86400)):
        with tempfile.TemporaryDirectory() as tmp:
            chat_id = "555000111"
            mod = load_module(tmp, chat_id)
            (mod.REPO / "state" / "rooms" / chat_id).mkdir(parents=True, exist_ok=True)
            now = int(time.time())
            weekly_resets_at = now + ahead
            cache_path = mod.REPO / "state" / "usage_cache.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({
                "seven_day": {"utilization": 1.0, "status": "rejected",
                              "resetsAt": weekly_resets_at,
                              "isUsingOverage": False, "updatedAt": now}}))
            mod._park_room_for_rate_limit()
            marker = mod.REPO / "state" / "rooms" / chat_id / ".rate_limited_until"
            written = int(marker.read_text().strip()) if marker.exists() else None
            if written != weekly_resets_at:
                failures.append(
                    f"case 4 ({label}): weekly reset {weekly_resets_at} should be "
                    f"used verbatim, got {written} (diff from now: "
                    f"{(written or now) - now}s; ~300 means the 5-minute retry loop is back)")
            else:
                print(f"ok: weekly limit resetting in {label} parks until the real reset")

    # Case 5: spawn_claude.sh's own post-exit calculation (two inline python
    # blocks) had the same 6h bound. Run both against a stream whose last
    # rate_limit_event is a weekly limit resetting 15h away.
    import re, subprocess
    src = open(os.path.join(SCRIPTS, "spawn_claude.sh")).read()
    blocks = [b for b in re.findall(r"python3 <<'PYEOF'\n(import json, os, time\n.*?)\nPYEOF", src, re.S)
              if "resume = resets if" in b]
    if len(blocks) != 2:
        failures.append(f"case 5: expected 2 park-calculation blocks in spawn_claude.sh, found {len(blocks)}")
    for i, (block, status, util) in enumerate(zip(blocks, ("rejected", "allowed_warning"), (1.0, 0.995))):
        now = int(time.time())
        resets = now + 15 * 3600
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                "status": status, "resetsAt": resets, "rateLimitType": "seven_day",
                "utilization": util}}) + "\n")
            stream = f.name
        out = subprocess.run([sys.executable, "-c", block], capture_output=True, text=True,
                             env={**os.environ, "STREAM_F": stream}).stdout.strip()
        os.unlink(stream)
        got = int(out.split("\t")[0]) if out else None
        which = ("hard-limit exit", "99% warning")[i]
        if got != resets:
            failures.append(f"case 5 ({which}): spawn_claude.sh parked until {got}, "
                            f"expected the weekly reset {resets} (output {out!r})")
        else:
            print(f"ok: spawn_claude.sh ({which}) parks a 15h-away weekly limit until its reset")

    # Case 6: the in-chat "⏸ Rate limited" message is sent at most once an
    # hour per room, so even a fallback retry loop can't spam the chat.
    with tempfile.TemporaryDirectory() as tmp:
        chat_id = "555000111"
        mod = load_module(tmp, chat_id)
        (mod.REPO / "state" / "rooms" / chat_id).mkdir(parents=True, exist_ok=True)
        fresh = not mod._rate_notice_recent()
        mod._mark_rate_notice()
        quiet = mod._rate_notice_recent()
        (mod.REPO / "state" / "rooms" / chat_id / ".rate_notice_at").write_text(
            str(int(time.time()) - mod.RATE_NOTICE_QUIET_S - 5))
        expired = not mod._rate_notice_recent()
        if fresh and quiet and expired:
            print("ok: rate-limit notice is sent once, then held back for an hour")
        else:
            failures.append(f"case 6: notice throttle wrong (fresh={fresh}, quiet={quiet}, expired={expired})")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: _park_room_for_rate_limit always uses the real computed "
          "reset time, never silently discards it")


if __name__ == "__main__":
    main()
