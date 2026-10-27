#!/usr/bin/env python3
"""Exhaustive offline test for scripts/approved_users.py.

Network-free and isolated: points APPROVED_USERS_FILE at a tmp file so the
real state/approved_users.json is never touched. Covers add / list / flatten /
remove, idempotency, username refresh, per-room isolation, atomic-write
round-trip, and malformed-file tolerance.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))

_TMP = tempfile.NamedTemporaryFile(prefix="approved_users_test_", suffix=".json", delete=False)
_TMP.close()
os.environ["APPROVED_USERS_FILE"] = _TMP.name
_TMPP = tempfile.NamedTemporaryFile(prefix="pending_users_test_", suffix=".json", delete=False)
_TMPP.close()
os.environ["PENDING_USERS_FILE"] = _TMPP.name

import approved_users  # noqa: E402  (after env is set)

FAILS = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def reset():
    for p in (_TMP.name, _TMPP.name):
        if os.path.exists(p):
            os.remove(p)


try:
    ROOM_A = "-1001000000002"
    ROOM_B = "-1001000000003"

    # ---- empty / missing file -------------------------------------------
    reset()
    check("missing file -> empty list", approved_users.list_room(ROOM_A) == [])
    check("missing file -> empty flatten", approved_users.flatten() == "")

    # ---- add + list -----------------------------------------------------
    approved_users.add(ROOM_A, 111222333, "test_user_1")
    got = approved_users.list_room(ROOM_A)
    check("add then list returns one", len(got) == 1)
    check("add stores id", got[0]["id"] == 111222333)
    check("add stores username", got[0]["username"] == "test_user_1")
    check("add stamps approved_at", bool(got[0].get("approved_at")))

    # ---- flatten format -------------------------------------------------
    check("flatten is chat:uid", approved_users.flatten() == f"{ROOM_A}:111222333")

    # ---- idempotent by id, refreshes username ---------------------------
    approved_users.add(ROOM_A, 111222333, "new_handle")
    got = approved_users.list_room(ROOM_A)
    check("re-add same id does not duplicate", len(got) == 1)
    check("re-add refreshes username", got[0]["username"] == "new_handle")

    # ---- leading @ is stripped -----------------------------------------
    approved_users.add(ROOM_A, 444555666, "@withat")
    got = {u["id"]: u for u in approved_users.list_room(ROOM_A)}
    check("username @ stripped", got[444555666]["username"] == "withat")
    check("two users in room A", len(got) == 2)

    # ---- per-room isolation --------------------------------------------
    approved_users.add(ROOM_B, 999888777, "sam")
    check("room B has its own user", [u["id"] for u in approved_users.list_room(ROOM_B)] == [999888777])
    check("room A unaffected by room B add", len(approved_users.list_room(ROOM_A)) == 2)
    pairs = set(approved_users.flatten().split("\n"))
    check("flatten covers both rooms", pairs == {f"{ROOM_A}:111222333", f"{ROOM_A}:444555666", f"{ROOM_B}:999888777"})

    # ---- on-disk shape is valid JSON map -------------------------------
    with open(_TMP.name) as f:
        disk = json.load(f)
    check("disk is a dict keyed by chat", set(disk.keys()) == {ROOM_A, ROOM_B})

    # ---- remove ---------------------------------------------------------
    approved_users.remove(ROOM_A, 111222333)
    check("remove drops the user", [u["id"] for u in approved_users.list_room(ROOM_A)] == [444555666])
    check("remove leaves the other room", [u["id"] for u in approved_users.list_room(ROOM_B)] == [999888777])

    # ---- removing the last user drops the room key ---------------------
    approved_users.remove(ROOM_B, 999888777)
    with open(_TMP.name) as f:
        disk = json.load(f)
    check("emptied room key removed", ROOM_B not in disk)

    # ---- remove of unknown id is a no-op -------------------------------
    approved_users.remove(ROOM_A, 123)  # not present
    check("remove unknown id is harmless", [u["id"] for u in approved_users.list_room(ROOM_A)] == [444555666])

    # ---- malformed file tolerated --------------------------------------
    with open(_TMP.name, "w") as f:
        f.write("{ this is not json")
    check("malformed file -> empty list", approved_users.list_room(ROOM_A) == [])
    check("malformed file -> empty flatten", approved_users.flatten() == "")
    # ...and a write recovers it
    approved_users.add(ROOM_A, 1010101010, "recover")
    check("write recovers from malformed", [u["id"] for u in approved_users.list_room(ROOM_A)] == [1010101010])

    # ---- pending list: add / list / remove / isolation ------------------
    reset()
    approved_users.pending_add(ROOM_A, 700700700)
    approved_users.pending_add(ROOM_A, 800800800)
    pend = [u["id"] for u in approved_users.pending_list_room(ROOM_A)]
    check("pending_add records, most-recent-first", pend == [800800800, 700700700])
    check("pending isolated per room", approved_users.pending_list_room(ROOM_B) == [])

    approved_users.pending_add(ROOM_A, 700700700)  # re-add bumps to front, no dup
    pend = [u["id"] for u in approved_users.pending_list_room(ROOM_A)]
    check("pending re-add dedups + bumps front", pend == [700700700, 800800800])

    # an approved user is hidden from pending (the two lists never double-show)
    approved_users.add(ROOM_A, 700700700, "now_member")
    pend = [u["id"] for u in approved_users.pending_list_room(ROOM_A)]
    check("approved id hidden from pending", pend == [800800800])

    approved_users.pending_remove(ROOM_A, 800800800)
    check("pending_remove drops it", approved_users.pending_list_room(ROOM_A) == [])

    # ---- pending cap ----------------------------------------------------
    reset()
    for i in range(approved_users.PENDING_CAP + 5):
        approved_users.pending_add(ROOM_B, 900000000 + i)
    capped = approved_users.pending_list_room(ROOM_B)
    check("pending list is capped", len(capped) == approved_users.PENDING_CAP)
    check("cap keeps the most recent", capped[0]["id"] == 900000000 + approved_users.PENDING_CAP + 4)

    # ---- move semantics (what commands/users.sh sequences) --------------
    reset()
    approved_users.pending_add(ROOM_A, 555000)                               # they messaged
    approved_users.add(ROOM_A, 555000, "x"); approved_users.pending_remove(ROOM_A, 555000)  # approve
    check("approve moves pending -> member",
          [u["id"] for u in approved_users.list_room(ROOM_A)] == [555000]
          and approved_users.pending_list_room(ROOM_A) == [])
    approved_users.remove(ROOM_A, 555000); approved_users.pending_add(ROOM_A, 555000)        # remove (demote)
    check("remove moves member -> pending",
          approved_users.list_room(ROOM_A) == []
          and [u["id"] for u in approved_users.pending_list_room(ROOM_A)] == [555000])

    # ---- arg-type coercion + flatten format contract --------------------
    # The gate matches " <chat>:<uid> " against flatten output, so the exact
    # "chat:uid" shape (incl. negative chat ids) is a contract worth pinning.
    reset()
    approved_users.add("-1001234567890", "424242", "Mixed_Case")  # str chat + str uid
    check("str chat_id + str user_id coerced to int",
          approved_users.list_room("-1001234567890")[0]["id"] == 424242)
    check("flatten emits exact chat:uid for negative chat",
          approved_users.flatten() == "-1001234567890:424242")

    # ---- idempotent add: keep approved_at, refresh username -------------
    reset()
    _orig_clock = approved_users._now_iso
    approved_users._now_iso = lambda: "2026-01-01T00:00:00Z"
    approved_users.add(ROOM_A, 1, "first")
    approved_users._now_iso = lambda: "2099-12-31T23:59:59Z"
    approved_users.add(ROOM_A, 1, "second")          # re-add, newer clock
    approved_users._now_iso = _orig_clock
    row = approved_users.list_room(ROOM_A)[0]
    check("re-add preserves original approved_at", row["approved_at"] == "2026-01-01T00:00:00Z")
    check("re-add refreshes the username", row["username"] == "second")
    approved_users.add(ROOM_A, 1, "")                # re-add with no username
    check("re-add with empty username keeps the old one",
          approved_users.list_room(ROOM_A)[0]["username"] == "second")

    # ---- malformed file entries are filtered, never crash --------------
    reset()
    with open(_TMP.name, "w") as f:
        json.dump({ROOM_A: [
            {"id": 10, "username": "ok"},
            {"username": "no_id"},          # missing id
            "not_a_dict",                    # junk entry
            {"id": 0, "username": "zero"},   # id 0 is falsy -> filtered
        ], ROOM_B: "not_a_list"}, f)
    check("list_room keeps only well-formed, truthy-id entries",
          [u["id"] for u in approved_users.list_room(ROOM_A)] == [10])
    check("flatten skips malformed entries + non-list rooms",
          approved_users.flatten() == f"{ROOM_A}:10")
    check("non-list room value reads as empty", approved_users.list_room(ROOM_B) == [])

    # ---- remove is a harmless no-op for absent room / id ---------------
    reset()
    approved_users.remove(ROOM_A, 999)               # room doesn't exist yet
    approved_users.add(ROOM_A, 5, "x")
    approved_users.remove(ROOM_A, 12345)             # wrong id
    check("remove of absent id leaves the rest",
          [u["id"] for u in approved_users.list_room(ROOM_A)] == [5])

    # ---- 64-bit (>2^32) Telegram ids round-trip ------------------------
    reset()
    big = 7_000_000_000_123
    approved_users.add(ROOM_A, big, "whale")
    approved_users.pending_add(ROOM_B, big + 1)
    check("64-bit approved id round-trips", approved_users.list_room(ROOM_A)[0]["id"] == big)
    check("64-bit pending id round-trips",
          approved_users.pending_list_room(ROOM_B)[0]["id"] == big + 1)
    check("flatten emits the full 64-bit id", approved_users.flatten() == f"{ROOM_A}:{big}")

    # ---- username @-stripping (post-refactor re-confirm) ---------------
    reset()
    approved_users.add(ROOM_A, 2, "@HandleWithAt")
    check("add strips a leading @", approved_users.list_room(ROOM_A)[0]["username"] == "HandleWithAt")

    # ---- pending edge cases --------------------------------------------
    reset()
    approved_users.pending_add(ROOM_A, 1)
    approved_users.pending_remove(ROOM_A, 1)         # remove the only entry
    with open(_TMPP.name) as f:
        check("emptied pending room key is dropped", ROOM_A not in json.load(f))

    reset()
    approved_users.pending_add(ROOM_A, 0)            # id 0 is falsy
    check("pending id 0 hidden from the list", approved_users.pending_list_room(ROOM_A) == [])

    reset()
    with open(_TMPP.name, "w") as f:
        f.write("}{ broken json")
    check("malformed pending file -> empty list", approved_users.pending_list_room(ROOM_A) == [])
    approved_users.pending_add(ROOM_A, 77)
    check("pending write recovers from a malformed file",
          [u["id"] for u in approved_users.pending_list_room(ROOM_A)] == [77])

    # pending_remove on a missing id / non-list room is a no-op
    reset()
    approved_users.pending_remove(ROOM_A, 404)       # nothing there
    with open(_TMPP.name, "w") as f:
        json.dump({ROOM_A: "not_a_list"}, f)
    approved_users.pending_remove(ROOM_A, 1)         # non-list room
    check("pending_remove tolerates absent/non-list", approved_users.pending_list_room(ROOM_A) == [])

    # approved + pending hold the same id: approved wins, pending hides it
    reset()
    approved_users.pending_add(ROOM_A, 42)
    approved_users.add(ROOM_A, 42, "dual")
    check("approved id never double-shows in pending",
          [u["id"] for u in approved_users.list_room(ROOM_A)] == [42]
          and approved_users.pending_list_room(ROOM_A) == [])

finally:
    reset()

if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("\nall passed")
