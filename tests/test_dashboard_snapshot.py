#!/usr/bin/env python3
"""
test_dashboard_snapshot.py - unit tests for scripts/dashboard.py's
build_snapshot() and its helper functions.

Deliberately does NOT start the HTTP server or touch a real port:
build_snapshot() is a plain function of (repo_root), fed a temp dir with
synthetic state/rooms/ and state/metrics.ndjson, so this stays as fast
and hermetic as the rest of tests/run.sh.

Run: python3 tests/test_dashboard_snapshot.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import metrics  # noqa: E402
import dashboard  # noqa: E402


def main():
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "state" / "rooms" / "-100").mkdir(parents=True)
        (repo / "state" / "rooms" / "-100" / "label").write_text("Test Room")
        (repo / "state" / "rooms" / "-100" / ".spawn_version").write_text("abc1234")
        (repo / "state" / "rooms" / "555").mkdir(parents=True)
        (repo / "state" / "rooms" / "555" / ".spawn_version").write_text("def5678")
        (repo / "state" / ".listener_started_at").write_text(str(int(time.time()) - 3600))
        (repo / "state" / ".listener_version").write_text("abc1234")

        metrics.METRICS_FILE = repo / "state" / "metrics.ndjson"
        for i in range(5):
            metrics.emit("turn_end", room="-100", duration_ms=1000 + i * 200,
                         is_error=(i == 4), version="abc1234")
        metrics.emit("crash", room="-100", exit_code=1)
        metrics.emit("rate_limit_hit", room="555", limit_type="5-hour session limit",
                     resume_at=int(time.time() + 3600))
        metrics.emit("rate_limit_resume", room="555")
        metrics.emit("tg_429", room="-100", retry_after=12, backoff_level=1)
        metrics.emit("dequeue", room="-100", count=3)
        metrics.emit("dequeue", room="-100", count=1)
        metrics.emit("restart_auto")
        metrics.emit("restart_manual")

        snap = dashboard.build_snapshot(repo)

        # ---- top-level shape ----
        if snap["uptime_s"] is None or not (3500 < snap["uptime_s"] < 3700):
            failures.append(f"uptime_s wrong: expected ~3600, got {snap['uptime_s']!r}")
        else:
            print(f"ok: uptime computed from .listener_started_at ({snap['uptime_s']:.0f}s)")

        # ---- rooms ----
        rooms_by_id = {r["chat_id"]: r for r in snap["rooms"]}
        if set(rooms_by_id) != {"-100", "555"}:
            failures.append(f"expected rooms -100 and 555, got {set(rooms_by_id)}")
        elif rooms_by_id["-100"]["label"] != "Test Room":
            failures.append(f"room -100 should use its label file, got "
                             f"{rooms_by_id['-100']['label']!r}")
        elif rooms_by_id["555"]["label"] != "555":
            failures.append(f"room 555 (no label file) should fall back to its "
                             f"chat_id, got {rooms_by_id['555']['label']!r}")
        else:
            print("ok: room label falls back to chat_id when no label file exists")

        # ---- stats ----
        s = snap["stats"]
        checks = [
            ("turns_total", 5), ("turns_error", 1), ("crashes", 1),
            ("rate_limit_hits", 1), ("rate_limit_resumes", 1), ("tg_429s", 1),
            ("restarts_auto", 1), ("restarts_manual", 1),
        ]
        for key, expected in checks:
            if s.get(key) != expected:
                failures.append(f"stats.{key}: expected {expected}, got {s.get(key)!r}")
        if not failures:
            print("ok: stats correctly aggregate every metric event type")

        if s["avg_duration_ms"] != 1400.0:
            failures.append(f"avg_duration_ms: expected 1400.0, got {s['avg_duration_ms']!r}")
        else:
            print("ok: avg_duration_ms correctly averages turn_end durations "
                  "(1000,1200,1400,1600,1800 -> 1400)")

        if s["avg_batch_size"] != 2.0:
            failures.append(f"avg_batch_size: expected 2.0 (avg of 3,1), got "
                             f"{s['avg_batch_size']!r}")
        else:
            print("ok: avg_batch_size correctly averages dequeue counts")

        # ---- timeseries: the most recent bucket must carry everything ----
        # (all events were just emitted "now")
        last_bucket = snap["timeseries"][-1]
        if last_bucket["turns"] != 5:
            failures.append(f"last timeseries bucket: expected turns=5, got "
                             f"{last_bucket['turns']!r} (bucket={last_bucket!r})")
        elif last_bucket["errors"] != 1:
            failures.append(f"last timeseries bucket: expected errors=1, got "
                             f"{last_bucket['errors']!r}")
        elif last_bucket["tg429"] != 1:
            failures.append(f"last timeseries bucket: expected tg429=1, got "
                             f"{last_bucket['tg429']!r}")
        elif last_bucket["avg_duration_ms"] != 1400.0:
            failures.append(f"last timeseries bucket: expected avg_duration_ms=1400.0, "
                             f"got {last_bucket['avg_duration_ms']!r}")
        else:
            print("ok: the most recent timeseries bucket carries all just-emitted events")

        # An earlier, empty bucket must not leak data from the populated one.
        if snap["timeseries"][0]["turns"] != 0:
            failures.append("an old, empty bucket unexpectedly shows turns > 0; "
                             "bucketing is bleeding across bucket boundaries")
        else:
            print("ok: empty buckets stay empty (no cross-bucket leakage)")

        # ---- versions: "chats by listener version" breakdown ----
        if snap["current_version"] != "abc1234":
            failures.append(f"current_version: expected 'abc1234', got "
                             f"{snap['current_version']!r}")
        versions_by_sha = {v["version"]: v for v in snap["versions"]}
        if set(versions_by_sha) != {"abc1234", "def5678"}:
            failures.append(f"expected versions abc1234 and def5678, got "
                             f"{set(versions_by_sha)}")
        elif versions_by_sha["abc1234"]["room_count"] != 1 or \
                versions_by_sha["abc1234"]["turns"] != 5:
            failures.append(f"version abc1234: expected room_count=1, turns=5, "
                             f"got {versions_by_sha['abc1234']!r}")
        elif versions_by_sha["def5678"]["room_count"] != 1 or \
                versions_by_sha["def5678"]["turns"] != 0:
            failures.append(f"version def5678 (no turn_end events): expected "
                             f"room_count=1, turns=0, got {versions_by_sha['def5678']!r}")
        elif versions_by_sha["abc1234"]["rooms"][0]["label"] != "Test Room":
            failures.append("version abc1234's room list should include Test Room, "
                             f"got {versions_by_sha['abc1234']['rooms']!r}")
        else:
            print("ok: versions breakdown groups rooms by .spawn_version and "
                  "cross-references turn_end counts per version")

        # ---- range param: a narrower range must shrink the window/buckets ----
        snap_1h = dashboard.build_snapshot(repo, "1h")
        if snap_1h["window_s"] != 3600 or snap_1h["bucket_s"] != 60:
            failures.append(f"range='1h': expected window_s=3600 bucket_s=60, "
                             f"got window_s={snap_1h['window_s']!r} bucket_s={snap_1h['bucket_s']!r}")
        elif snap_1h["range"] != "1h":
            failures.append(f"range='1h': expected echoed range field, got {snap_1h['range']!r}")
        else:
            print("ok: build_snapshot(repo, '1h') narrows the window and bucket width")

        snap_bad_range = dashboard.build_snapshot(repo, "not-a-real-range")
        if snap_bad_range["range"] != dashboard.DEFAULT_RANGE:
            failures.append(f"an unknown range key should fall back to "
                             f"DEFAULT_RANGE, got {snap_bad_range['range']!r}")
        else:
            print("ok: an unrecognised range key falls back to the default range")

    # ---- _room_status: status mapping, independent of build_snapshot ----
    now = time.time()
    status_checks = [
        ({"state": "idle"}, None, "good"),
        ({"state": "tool"}, None, "busy"),
        ({"state": "streaming"}, None, "busy"),
        ({"state": "awaiting_result"}, None, "busy"),
        ({"state": "no_session"}, None, "neutral"),
        ({"state": "no_spawn"}, None, "neutral"),
        ({"state": "idle"}, now + 3600, "warning"),   # rate limit wins over idle
        ({"state": "tool"}, now + 3600, "warning"),    # rate limit wins over busy too
        ({"state": "idle"}, now - 10, "good"),         # EXPIRED rate limit, must not stick
    ]
    for state_info, rlu, expected in status_checks:
        got = dashboard._room_status(state_info, rlu)
        if got != expected:
            failures.append(f"_room_status({state_info!r}, {rlu!r}): "
                             f"expected {expected!r}, got {got!r}")
    if not any("_room_status" in f for f in failures):
        print("ok: _room_status maps every spawn_state + rate-limit combination correctly")

    # ---- _percentile ----
    if dashboard._percentile([], 0.95) is not None:
        failures.append("_percentile of an empty list must be None")
    elif dashboard._percentile([1, 2, 3, 4, 5], 0.0) != 1:
        failures.append("_percentile at 0.0 should return the minimum")
    elif dashboard._percentile([1, 2, 3, 4, 5], 1.0) != 5:
        failures.append("_percentile at 1.0 should return the maximum")
    else:
        print("ok: _percentile handles empty input and the 0.0/1.0 edges correctly")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: dashboard snapshot aggregation is correct across rooms, stats, "
          "timeseries bucketing, and status mapping")


if __name__ == "__main__":
    main()
