#!/usr/bin/env python3
"""approved_users.py: per-room user allowlist + pending list for the bot.

The bot OWNER (TELEGRAM_CHAT_ID) is always allowed in every room and is NOT
stored here. This module manages TWO per-room lists the Users menu moves people
between:

  - APPROVED ("Members"): users allowed to talk to the bot in that room. The
    listener reads this LIVE (refresh_approved / sender_allowed in listener.sh),
    so an approve/remove takes effect on the user's next message, no restart.
    State: state/approved_users.json  -> { "<chat_id>": [ {id, username, approved_at} ] }

  - PENDING ("⏳ messaged, not allowed yet"): users who messaged a room they
    aren't approved for. The listener's drop path captures them here so the
    owner can one-tap approve from the menu (the native contact picker can't
    surface group-only members). Capped + most-recent-first.
    State: state/pending_users.json   -> { "<chat_id>": [ {id, last_seen} ] }

The module is PURE storage for the two lists (no cross-effects); the handler
(commands/users.sh) owns the "move between lists" workflow: approve = approved
add + pending remove; remove = approved remove + pending add (demote).
pending_list_room hides anyone currently approved, so an approved user never
shows as pending even if a stale entry lingers.

DESIGN AXIOM: nothing here hardcodes a chat id or user id; every entry is keyed
by ids handed in at runtime by the menu / the listener's drop path.

CLI (bash callers: listener gate, commands/users.sh):
    flatten                                -> "<chat>:<uid>" per approved pair
    add          <chat> <uid> [username]   -> approve (idempotent, atomic)
    remove       <chat> <uid>              -> revoke (atomic)
    list         <chat>                    -> TSV uid\tusername\tapproved_at
    pending-add  <chat> <uid>              -> record a pending user (atomic)
    pending-remove <chat> <uid>            -> drop a pending user (atomic)
    pending-list <chat>                    -> TSV uid\tlast_seen (excludes approved)

Module: list_room / add / remove / flatten and
        pending_list_room / pending_add / pending_remove.

State paths override via APPROVED_USERS_FILE / PENDING_USERS_FILE (tests).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING_CAP = 15  # keep the pending list bounded (most-recent-first)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _approved_file() -> str:
    # Read env per call (not at import) so tests can redirect without import
    # gymnastics. The listener subprocess never sets it -> real state file.
    return os.environ.get("APPROVED_USERS_FILE") or os.path.join(
        REPO_ROOT, "state", "approved_users.json")


def _pending_file() -> str:
    return os.environ.get("PENDING_USERS_FILE") or os.path.join(
        REPO_ROOT, "state", "pending_users.json")


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(path: str, d: dict) -> None:
    """Atomic write (tmp + os.replace) so the listener never reads a half file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


# ---- approved ("Members") -------------------------------------------------

def list_room(chat_id: str) -> list:
    room = _load(_approved_file()).get(str(chat_id)) or []
    return [u for u in room if isinstance(u, dict) and u.get("id")]


def add(chat_id: str, user_id, username: str = "") -> None:
    """Approve user_id for chat_id. Idempotent by id; refreshes the @username
    when a newer one is supplied. Atomic."""
    chat_id = str(chat_id)
    user_id = int(user_id)
    username = (username or "").lstrip("@")
    path = _approved_file()
    d = _load(path)
    room = d.get(chat_id)
    if not isinstance(room, list):
        room = []
    for u in room:
        if isinstance(u, dict) and int(u.get("id", 0)) == user_id:
            if username:
                u["username"] = username
            d[chat_id] = room
            _save(path, d)
            return
    room.append({"id": user_id, "username": username, "approved_at": _now_iso()})
    d[chat_id] = room
    _save(path, d)


def remove(chat_id: str, user_id) -> None:
    """Revoke user_id for chat_id. Drops the room key when it empties. Atomic."""
    chat_id = str(chat_id)
    user_id = int(user_id)
    path = _approved_file()
    d = _load(path)
    room = d.get(chat_id)
    if not isinstance(room, list):
        return
    room = [u for u in room
            if not (isinstance(u, dict) and int(u.get("id", 0)) == user_id)]
    if room:
        d[chat_id] = room
    else:
        d.pop(chat_id, None)
    _save(path, d)


def flatten() -> str:
    """One '<chat_id>:<user_id>' token per APPROVED pair, newline-joined."""
    out = []
    for chat_id, room in _load(_approved_file()).items():
        if not isinstance(room, list):
            continue
        for u in room:
            if isinstance(u, dict) and u.get("id"):
                out.append(f"{chat_id}:{int(u['id'])}")
    return "\n".join(out)


# ---- pending ("⏳ messaged, not allowed yet") -----------------------------

def pending_list_room(chat_id: str) -> list:
    """Pending users for a room, EXCLUDING anyone currently approved (so an
    approved user never lingers in the pending section)."""
    chat_id = str(chat_id)
    approved_ids = {int(u["id"]) for u in list_room(chat_id)}
    room = _load(_pending_file()).get(chat_id) or []
    out = []
    for u in room:
        if isinstance(u, dict) and u.get("id") and int(u["id"]) not in approved_ids:
            out.append(u)
    return out


def pending_add(chat_id: str, user_id) -> None:
    """Record a pending user (dedup by id, most-recent-first, capped). Atomic."""
    chat_id = str(chat_id)
    user_id = int(user_id)
    path = _pending_file()
    d = _load(path)
    room = d.get(chat_id)
    if not isinstance(room, list):
        room = []
    room = [u for u in room
            if not (isinstance(u, dict) and int(u.get("id", 0)) == user_id)]
    room.insert(0, {"id": user_id, "last_seen": _now_iso()})
    d[chat_id] = room[:PENDING_CAP]
    _save(path, d)


def pending_remove(chat_id: str, user_id) -> None:
    """Drop a pending user. Drops the room key when it empties. Atomic."""
    chat_id = str(chat_id)
    user_id = int(user_id)
    path = _pending_file()
    d = _load(path)
    room = d.get(chat_id)
    if not isinstance(room, list):
        return
    room = [u for u in room
            if not (isinstance(u, dict) and int(u.get("id", 0)) == user_id)]
    if room:
        d[chat_id] = room
    else:
        d.pop(chat_id, None)
    _save(path, d)


def _cli(argv: list) -> int:
    if not argv:
        sys.stderr.write("usage: approved_users.py {flatten|add|remove|list|"
                         "pending-add|pending-remove|pending-list} ...\n")
        return 2
    cmd = argv[0]
    if cmd == "flatten":
        print(flatten())
    elif cmd == "add":
        add(argv[1], argv[2], argv[3] if len(argv) > 3 else "")
    elif cmd == "remove":
        remove(argv[1], argv[2])
    elif cmd == "list":
        for u in list_room(argv[1]):
            print(f"{u.get('id')}\t{u.get('username', '')}\t{u.get('approved_at', '')}")
    elif cmd == "pending-add":
        pending_add(argv[1], argv[2])
    elif cmd == "pending-remove":
        pending_remove(argv[1], argv[2])
    elif cmd == "pending-list":
        for u in pending_list_room(argv[1]):
            print(f"{u.get('id')}\t{u.get('last_seen', '')}")
    else:
        sys.stderr.write(f"unknown command: {cmd}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
