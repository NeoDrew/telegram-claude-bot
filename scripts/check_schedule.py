#!/usr/bin/env python3
"""
check_schedule.py - called by listener.sh each poll iteration.

Reads the global state/scheduled.json, returns due tasks on stdout as
(target_chat_id, prompt) tuples separated by sentinels, and rewrites the
file with fired tasks removed (one-shot) or rescheduled (recurring).

Usage:
    python3 check_schedule.py <scheduled.json path> <now_epoch_seconds>

The path arg points at the global state/scheduled.json (one file, all
rooms). Each task entry MUST carry an explicit `target_chat_id`. Entries
without that field are dropped with a stderr warning (back-compat safety
net; pre-refactor per-room files no longer feed this script).

Output format:
    <chat_id>\x1f<prompt>\x1e<chat_id>\x1f<prompt>\x1e...

\x1f (unit separator) splits chat_id from prompt within a record.
\x1e (record separator) splits records. Both are safe vs arbitrary prompt
content because they never appear in user text.
"""

import json
import os
import sys
from datetime import datetime, timezone

UNIT = "\x1f"
RECORD = "\x1e"


def next_recurring(entry: dict, now: float) -> float:
    interval = entry.get("interval_seconds", 300)
    last = entry.get("last_fired_at", entry["fire_at_epoch"])
    nxt = last + interval
    if nxt <= now:
        # Long sleep catch-up: skip ahead one interval from "now" rather
        # than firing N missed ticks back-to-back.
        nxt = now + interval
    return nxt


def main() -> int:
    if len(sys.argv) < 3:
        return 0

    sched_path = sys.argv[1]
    try:
        now = float(sys.argv[2])
    except ValueError:
        return 0

    try:
        with open(sched_path) as f:
            tasks = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0

    if not isinstance(tasks, list):
        return 0

    due = []
    remaining = []

    for task in tasks:
        if not isinstance(task, dict):
            continue
        fire_at = task.get("fire_at_epoch")
        if fire_at is None:
            remaining.append(task)
            continue

        if float(fire_at) <= now:
            prompt = task.get("prompt", "")
            target = str(task.get("target_chat_id", "")).strip()
            if prompt and target:
                due.append((target, prompt))
            elif prompt and not target:
                print(f"check_schedule.py: REFUSED task id="
                      f"{task.get('id', '?')[:8]} - no target_chat_id; dropping",
                      file=sys.stderr)
                continue

            if task.get("recurring", False):
                task["last_fired_at"] = fire_at
                task["fire_at_epoch"] = next_recurring(task, now)
                task["fire_at_human"] = datetime.fromtimestamp(
                    task["fire_at_epoch"], tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                remaining.append(task)
            # else: one-shot, drop it.
        else:
            remaining.append(task)

    if due:
        # Atomic write: readers can never see a torn/half-written file.
        # NOTE this does not serialise concurrent read-modify-write cycles
        # (a schedule_task.sh add racing this rewrite can still lose one
        # side's update); full mutual exclusion would need an flock around
        # both writers. Torn reads were the observed failure mode.
        tmp = f"{sched_path}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(remaining, f, indent=2)
        os.replace(tmp, sched_path)
        sys.stdout.write(RECORD.join(f"{c}{UNIT}{p}" for c, p in due))

    return 0


if __name__ == "__main__":
    sys.exit(main())
