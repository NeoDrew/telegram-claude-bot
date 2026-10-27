#!/usr/bin/env python3
"""
room_state.py: the single source of truth for a room's sessions.json.

Every reader/writer of a room's session state goes through here so the
schema lives in exactly ONE file. Before this, the shape was re-implemented
inline in ~7 scripts (cd, new, menu, status, rooms, listener, spawn_claude),
so changing the data model meant hunting down every json.load/json.dump.

Schema (flat, one session per room):

    {"active_cwd": "<absolute path>", "session_id": "<uuid or empty>"}

Back-compat: the old shape {"active_cwd", "sessions": {cwd: uuid}} is still
READ (session falls back to sessions[active_cwd]); the next write normalises
to the flat shape and drops the legacy map.

Usable two ways, both backed by the same functions so they can't drift:

  As a CLI (for pure-bash callers):
    room_state.py get-cwd      <sessions_file>      -> prints active_cwd (or the projects dir)
    room_state.py get-session  <sessions_file>      -> prints session_id (or "")
    room_state.py set          <sessions_file> [--cwd P] [--session S]
    room_state.py clear-session <sessions_file>     -> session_id = "" (keeps cwd)
    room_state.py init         <sessions_file>      -> create default if missing

  As a module (for callers already running python):
    import room_state
    cwd = room_state.get_cwd(path)
    room_state.write(path, cwd=..., session=...)

The JSON written is byte-for-byte what the old inline code produced
(indent=2, keys active_cwd + session_id) so a process still running the
pre-migration code reads it without a hiccup during cutover.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_config  # noqa: E402

DEFAULT_CWD = bot_config.projects_dir()


def _load(path: str) -> dict:
    """Read sessions.json, tolerating absence and malformed content."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def get_cwd(path: str) -> str:
    return _load(path).get("active_cwd") or DEFAULT_CWD


def get_session(path: str) -> str:
    d = _load(path)
    sid = d.get("session_id")
    if sid:
        return sid
    # Legacy shape: {active_cwd, sessions: {cwd: uuid}}.
    legacy = d.get("sessions")
    if isinstance(legacy, dict):
        return legacy.get(d.get("active_cwd", ""), "") or ""
    return ""


def write(path: str, cwd: str | None = None, session: str | None = None) -> dict:
    """Update the given fields and persist the normalised flat shape.

    Fields left as None are preserved from the current file (cwd defaults to
    the projects dir when absent; session defaults to ""). The legacy `sessions` map is
    dropped on write.
    """
    cur = _load(path)
    new_cwd = cwd if cwd is not None else (cur.get("active_cwd") or DEFAULT_CWD)
    new_sid = session if session is not None else get_session(path)
    out = {"active_cwd": new_cwd, "session_id": new_sid}
    # Atomic write (tmp + os.replace). A plain open(path, "w") truncates in
    # place, so a crash mid-write or a concurrent reader (spawn_claude.sh's
    # get-cwd/get-session run as separate invocations) could see a half file
    # and silently fall back to the projects dir with no session. Same pattern as
    # approved_users.py::_save.
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, path)
    return out


def clear_session(path: str) -> dict:
    return write(path, session="")


def init(path: str) -> dict:
    """Create the default file only if it does not already exist."""
    if not os.path.exists(path):
        return write(path, cwd=DEFAULT_CWD, session="")
    return _load(path)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="room_state.py")
    sub = p.add_subparsers(dest="action", required=True)
    for verb in ("get-cwd", "get-session", "clear-session", "init"):
        sp = sub.add_parser(verb)
        sp.add_argument("sessions_file")
    sp_set = sub.add_parser("set")
    sp_set.add_argument("sessions_file")
    sp_set.add_argument("--cwd", default=None)
    sp_set.add_argument("--session", default=None)

    a = p.parse_args(argv)
    f = a.sessions_file
    if a.action == "get-cwd":
        print(get_cwd(f))
    elif a.action == "get-session":
        print(get_session(f))
    elif a.action == "set":
        write(f, cwd=a.cwd, session=a.session)
    elif a.action == "clear-session":
        clear_session(f)
    elif a.action == "init":
        init(f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
