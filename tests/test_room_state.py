#!/usr/bin/env python3
"""Unit tests for scripts/room_state.py, the single source of truth for a
room's sessions.json. These are exhaustive on purpose: every other script
trusts this module to read/write the schema correctly, so a regression here
silently corrupts session continuity across every room.

Run directly (`python3 tests/test_room_state.py`) or via tests/run.sh.
No network, no real rooms: everything happens in a tmp dir.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
RS_CLI = SCRIPTS / "room_state.py"
sys.path.insert(0, str(SCRIPTS))
import room_state  # noqa: E402


def cli(*args):
    """Invoke room_state.py as the bash callers do; return (rc, stdout)."""
    p = subprocess.run(
        [sys.executable, str(RS_CLI), *args],
        capture_output=True, text=True)
    return p.returncode, p.stdout.rstrip("\n"), p.stderr


class TmpRoom(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rs_test_")
        self.f = os.path.join(self.dir, "sessions.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_raw(self, obj):
        with open(self.f, "w") as fh:
            json.dump(obj, fh, indent=2)


class TestModuleReads(TmpRoom):
    def test_missing_file_defaults(self):
        self.assertEqual(room_state.get_cwd(self.f), room_state.DEFAULT_CWD)
        self.assertEqual(room_state.get_session(self.f), "")

    def test_flat_shape_reads(self):
        self.write_raw({"active_cwd": "/Users/x/projects/foo", "session_id": "uuid-1"})
        self.assertEqual(room_state.get_cwd(self.f), "/Users/x/projects/foo")
        self.assertEqual(room_state.get_session(self.f), "uuid-1")

    def test_empty_active_cwd_falls_to_default(self):
        self.write_raw({"active_cwd": "", "session_id": ""})
        self.assertEqual(room_state.get_cwd(self.f), room_state.DEFAULT_CWD)

    def test_legacy_sessions_map_fallback(self):
        # Old shape: {active_cwd, sessions: {cwd: uuid}} with no session_id.
        self.write_raw({"active_cwd": "/p", "sessions": {"/p": "leg-uuid", "/q": "z"}})
        self.assertEqual(room_state.get_cwd(self.f), "/p")
        self.assertEqual(room_state.get_session(self.f), "leg-uuid")

    def test_legacy_map_no_match_returns_empty(self):
        self.write_raw({"active_cwd": "/p", "sessions": {"/other": "z"}})
        self.assertEqual(room_state.get_session(self.f), "")

    def test_session_id_wins_over_legacy_map(self):
        self.write_raw({"active_cwd": "/p", "session_id": "flat", "sessions": {"/p": "leg"}})
        self.assertEqual(room_state.get_session(self.f), "flat")

    def test_malformed_json_treated_as_empty(self):
        with open(self.f, "w") as fh:
            fh.write("{not json at all")
        self.assertEqual(room_state.get_cwd(self.f), room_state.DEFAULT_CWD)
        self.assertEqual(room_state.get_session(self.f), "")

    def test_non_dict_json_treated_as_empty(self):
        with open(self.f, "w") as fh:
            json.dump([1, 2, 3], fh)
        self.assertEqual(room_state.get_session(self.f), "")


class TestModuleWrites(TmpRoom):
    def test_write_both_fields_byte_identical_to_old_inline(self):
        room_state.write(self.f, cwd="/Users/x/projects/y", session="abc-123")
        got = Path(self.f).read_bytes()
        # The exact bytes the pre-refactor inline code produced.
        expect = json.dumps(
            {"active_cwd": "/Users/x/projects/y", "session_id": "abc-123"}, indent=2
        ).encode()
        self.assertEqual(got, expect)

    def test_write_only_session_preserves_cwd(self):
        room_state.write(self.f, cwd="/keep", session="s1")
        room_state.write(self.f, session="s2")
        self.assertEqual(room_state.get_cwd(self.f), "/keep")
        self.assertEqual(room_state.get_session(self.f), "s2")

    def test_write_only_cwd_preserves_session(self):
        room_state.write(self.f, cwd="/a", session="keepme")
        room_state.write(self.f, cwd="/b")
        self.assertEqual(room_state.get_cwd(self.f), "/b")
        self.assertEqual(room_state.get_session(self.f), "keepme")

    def test_write_normalises_and_drops_legacy_map(self):
        self.write_raw({"active_cwd": "/p", "sessions": {"/p": "leg"}})
        room_state.write(self.f, session="new")
        d = json.loads(Path(self.f).read_text())
        self.assertNotIn("sessions", d)
        self.assertEqual(d, {"active_cwd": "/p", "session_id": "new"})

    def test_clear_session_keeps_cwd(self):
        room_state.write(self.f, cwd="/here", session="live")
        room_state.clear_session(self.f)
        self.assertEqual(room_state.get_cwd(self.f), "/here")
        self.assertEqual(room_state.get_session(self.f), "")

    def test_init_creates_default_only_if_missing(self):
        room_state.init(self.f)
        self.assertTrue(os.path.exists(self.f))
        self.assertEqual(room_state.get_cwd(self.f), room_state.DEFAULT_CWD)
        # init must NOT clobber an existing file.
        room_state.write(self.f, cwd="/custom", session="keep")
        room_state.init(self.f)
        self.assertEqual(room_state.get_cwd(self.f), "/custom")
        self.assertEqual(room_state.get_session(self.f), "keep")

    def test_round_trips_stay_flat(self):
        for i in range(5):
            room_state.write(self.f, cwd=f"/d{i}", session=f"s{i}")
            d = json.loads(Path(self.f).read_text())
            self.assertEqual(set(d.keys()), {"active_cwd", "session_id"})


class TestCliParity(TmpRoom):
    """The CLI and the module must never drift: same backing functions."""

    def test_cli_get_cwd_default(self):
        rc, out, _ = cli("get-cwd", self.f)
        self.assertEqual(rc, 0)
        self.assertEqual(out, room_state.DEFAULT_CWD)

    def test_cli_set_then_get(self):
        cli("set", self.f, "--cwd", "/Users/x/projects/z", "--session", "sid-9")
        self.assertEqual(cli("get-cwd", self.f)[1], "/Users/x/projects/z")
        self.assertEqual(cli("get-session", self.f)[1], "sid-9")

    def test_cli_set_matches_module_bytes(self):
        cli("set", self.f, "--cwd", "/m", "--session", "n")
        cli_bytes = Path(self.f).read_bytes()
        other = os.path.join(self.dir, "mod.json")
        room_state.write(other, cwd="/m", session="n")
        self.assertEqual(cli_bytes, Path(other).read_bytes())

    def test_cli_clear_session(self):
        cli("set", self.f, "--cwd", "/k", "--session", "live")
        rc, _, _ = cli("clear-session", self.f)
        self.assertEqual(rc, 0)
        self.assertEqual(cli("get-session", self.f)[1], "")
        self.assertEqual(cli("get-cwd", self.f)[1], "/k")

    def test_cli_init_idempotent(self):
        cli("init", self.f)
        cli("set", self.f, "--cwd", "/keep", "--session", "x")
        cli("init", self.f)  # must not clobber
        self.assertEqual(cli("get-cwd", self.f)[1], "/keep")

    def test_cli_unknown_verb_errors(self):
        rc, _, err = cli("frobnicate", self.f)
        self.assertNotEqual(rc, 0)

    def test_cli_get_session_legacy(self):
        self.write_raw({"active_cwd": "/p", "sessions": {"/p": "leg"}})
        self.assertEqual(cli("get-session", self.f)[1], "leg")


if __name__ == "__main__":
    unittest.main(verbosity=2)
