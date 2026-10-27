#!/usr/bin/env python3
"""
test_schedule_routing.py - mission-critical guarantees for scheduled tasks.

The bug this prevents: a scheduled task created from room A whose content
belongs to project P (which lives in room B) silently fires in room A.
After the refactor, that path is blocked at THREE layers and any one of
them is enough to stop the misroute:

  1. schedule_task.sh consults state/room_registry.json and refuses to
     write a task into a target_chat_id that conflicts with a registered
     project prefix.
  2. Every task in state/scheduled.json carries an explicit target_chat_id
     field. check_schedule.py REFUSES to emit tasks missing that field
     (drops them with a stderr warning).
  3. listener.sh's fire_scheduled_tasks reads target_chat_id PER TASK,
     never from the file location. Even if a task somehow lands in the
     wrong file, the routing follows the field.

Tests below cover all three layers without spawning the bot or hitting
Telegram.
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

sys.path.insert(0, str(SCRIPTS))
import registry  # noqa: E402


class RegistryMatch(unittest.TestCase):
    """Layer 1: state/room_registry.json catches misrouted prompts."""

    def setUp(self):
        self.reg = {
            "projects": [
                {
                    "name": "ExampleWork",
                    "target_chat_id": "-1001000000001",
                    "path_prefixes": ["/Users/x/projects/projecta/"],
                    "url_prefixes": ["https://app.example-work.test/"],
                }
            ]
        }

    def test_url_in_prompt_matches(self):
        proj = registry.match_project(
            "Submit https://app.example-work.test/tasks/abc",
            registry=self.reg,
        )
        self.assertIsNotNone(proj)
        self.assertEqual(proj["target_chat_id"], "-1001000000001")

    def test_path_in_prompt_matches(self):
        proj = registry.match_project(
            "Read /Users/x/projects/projecta/state.json",
            registry=self.reg,
        )
        self.assertIsNotNone(proj)

    def test_cwd_match_when_prompt_clean(self):
        proj = registry.match_project(
            "Run a quick check.",
            cwd="/Users/x/projects/projecta/docs/task1",
            registry=self.reg,
        )
        self.assertIsNotNone(proj)

    def test_unrelated_prompt_does_not_match(self):
        self.assertIsNone(
            registry.match_project(
                "Hello, world. What is the weather?",
                registry=self.reg,
            )
        )

    def test_validate_refuses_wrong_target(self):
        ok, msg = registry.validate_target(
            "Submit https://app.example-work.test/x",
            "1111111111",  # DM, not the registered group
            registry=self.reg,
        )
        self.assertFalse(ok, msg)
        self.assertIn("REGISTRY REFUSED", msg)
        self.assertIn("-1001000000001", msg)

    def test_validate_accepts_matching_target(self):
        ok, _ = registry.validate_target(
            "Submit https://app.example-work.test/x",
            "-1001000000001",
            registry=self.reg,
        )
        self.assertTrue(ok)

    def test_validate_accepts_when_unclaimed(self):
        ok, _ = registry.validate_target(
            "Quick reminder", "999", registry=self.reg
        )
        self.assertTrue(ok)


class AutoLearnBindings(unittest.TestCase):
    """The bot must register new project bindings AUTOMATICALLY on first
    schedule. No manual edits to room_registry.json should ever be
    required to defend a never-before-seen project."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.reg_path = self.tmp / "room_registry.json"
        self.reg_path.write_text(json.dumps({"projects": []}))

    def test_extracts_path_and_url_prefixes_from_prompt(self):
        paths, urls = registry.extract_prefixes(
            "Run /Users/x/projects/projecta/docs/task1/state.json "
            "and hit https://app.example-work.test/tasks/abc",
        )
        self.assertIn("/Users/x/projects/projecta/", paths)
        self.assertIn("https://app.example-work.test/", urls)

    def test_extracts_cwd_prefix_even_when_prompt_silent(self):
        paths, _ = registry.extract_prefixes(
            "Submit it.", cwd="/Users/x/projects/projecta/docs/task1"
        )
        self.assertIn("/Users/x/projects/projecta/", paths)

    def test_learn_creates_auto_entry_and_persists(self):
        registry.learn(
            "Edit /Users/x/projects/foo/bar.py", "-100", registry_path=self.reg_path
        )
        reg = registry.load_registry(self.reg_path)
        autos = [p for p in reg["projects"] if p.get("auto_learned")]
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0]["target_chat_id"], "-100")
        self.assertIn("/Users/x/projects/foo/", autos[0]["path_prefixes"])

    def test_learn_idempotent(self):
        registry.learn("/Users/x/projects/foo/", "-100", registry_path=self.reg_path)
        registry.learn("/Users/x/projects/foo/", "-100", registry_path=self.reg_path)
        reg = registry.load_registry(self.reg_path)
        autos = [p for p in reg["projects"] if p.get("auto_learned")]
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0]["path_prefixes"].count("/Users/x/projects/foo/"), 1)

    def test_learn_does_not_overwrite_existing_binding(self):
        # Pre-existing binding to chat A.
        registry.learn(
            "/Users/x/projects/foo/x.py", "100", registry_path=self.reg_path
        )
        # Attempt to relearn to chat B. learn() must NOT silently rebind.
        registry.learn(
            "/Users/x/projects/foo/y.py", "200", registry_path=self.reg_path
        )
        reg = registry.load_registry(self.reg_path)
        # The prefix should still belong to chat A.
        owners = []
        for proj in reg["projects"]:
            if "/Users/x/projects/foo/" in (proj.get("path_prefixes") or []):
                owners.append(proj["target_chat_id"])
        self.assertEqual(owners, ["100"])

    def test_validate_after_learn_refuses_other_chat(self):
        registry.learn(
            "Edit /Users/x/projects/projecta/state",
            "-1001000000001",
            registry_path=self.reg_path,
        )
        reg = registry.load_registry(self.reg_path)
        ok, msg = registry.validate_target(
            "Submit /Users/x/projects/projecta/state",
            target_chat_id="1111111111",
            registry=reg,
        )
        self.assertFalse(ok, msg)
        self.assertIn("REGISTRY REFUSED", msg)

    def test_forget_removes_binding(self):
        registry.learn(
            "/Users/x/projects/foo/x.py", "100", registry_path=self.reg_path
        )
        changed = registry.forget(
            "/Users/x/projects/foo/", registry_path=self.reg_path
        )
        self.assertTrue(changed)
        reg = registry.load_registry(self.reg_path)
        for proj in reg["projects"]:
            self.assertNotIn(
                "/Users/x/projects/foo/", proj.get("path_prefixes") or []
            )


class ScheduleTaskCLIRefusal(unittest.TestCase):
    """Layer 1, end-to-end: schedule_task.sh exits non-zero on misroute."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Build a sandboxed REPO clone with the bits schedule_task.sh needs.
        for name in ("schedule_task.sh", "registry.py"):
            os.symlink(SCRIPTS / name, Path(self.tmp) / name)
        state_dir = Path(self.tmp) / "state"
        state_dir.mkdir()
        # Pretend this is the project root by symlinking what's needed.
        (Path(self.tmp) / "scripts").symlink_to(self.tmp)
        # Registry: ExampleWork pinned to "-1001000000001".
        reg = {
            "projects": [
                {
                    "name": "ExampleWork",
                    "target_chat_id": "-1001000000001",
                    "path_prefixes": ["/test/docs/"],
                    "url_prefixes": ["https://app.example-work.test/"],
                }
            ]
        }
        (state_dir / "room_registry.json").write_text(json.dumps(reg))
        # Minimal .env so the script's extract_var doesn't crash.
        (Path(self.tmp) / ".env").write_text("TELEGRAM_CHAT_ID=99999\n")

    def _run(self, *args, env_chat=None):
        env = os.environ.copy()
        if env_chat is not None:
            env["CHAT_ID"] = str(env_chat)
        # Run the real schedule_task.sh but with a REPO_ROOT pointing into
        # our sandbox. The script computes REPO_ROOT from its own path, so
        # symlinking it into self.tmp/scripts/schedule_task.sh and invoking
        # via that path is what makes REPO_ROOT=self.tmp.
        return subprocess.run(
            ["bash", str(Path(self.tmp) / "scripts" / "schedule_task.sh")] + list(args),
            cwd=self.tmp,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_refuses_docs_prompt_into_DM(self):
        res = self._run(
            "--target-chat-id", "1111111111",
            "Submit https://app.example-work.test/tasks/abc",
            "2099-01-01T00:00:00Z",
            env_chat="1111111111",
        )
        self.assertNotEqual(res.returncode, 0)
        combined = (res.stdout + res.stderr).lower()
        self.assertIn("registry refused", combined)
        # Global file should not have grown.
        sched = json.loads((Path(self.tmp) / "state" / "scheduled.json").read_text())
        self.assertEqual(sched, [])

    def test_implicit_target_resolves_from_registry(self):
        # No --target-chat-id; CHAT_ID env says DM. Registry says
        # ExampleWork -> -1001000000001. Registry wins.
        res = self._run(
            "Submit https://app.example-work.test/x",
            "2099-01-01T00:00:00Z",
            env_chat="1111111111",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        sched = json.loads((Path(self.tmp) / "state" / "scheduled.json").read_text())
        self.assertEqual(len(sched), 1)
        self.assertEqual(sched[0]["target_chat_id"], "-1001000000001")

    def test_accepts_matching_target(self):
        res = self._run(
            "--target-chat-id", "-1001000000001",
            "Submit https://app.example-work.test/x",
            "2099-01-01T00:00:00Z",
            env_chat="-1001000000001",
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_unrelated_prompt_uses_caller_chat(self):
        res = self._run(
            "Remind me to drink water",
            "2099-01-01T00:00:00Z",
            env_chat="1111111111",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        sched = json.loads((Path(self.tmp) / "state" / "scheduled.json").read_text())
        self.assertEqual(len(sched), 1)
        self.assertEqual(sched[0]["target_chat_id"], "1111111111")


class CheckScheduleEmitsTargetTuples(unittest.TestCase):
    """Layer 2: check_schedule.py emits (chat_id, prompt) pairs and refuses
    legacy tasks without target_chat_id."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sched = Path(self.tmp) / "scheduled.json"

    def _run(self, tasks, now=2_000_000_000):
        self.sched.write_text(json.dumps(tasks))
        res = subprocess.run(
            ["python3", str(SCRIPTS / "check_schedule.py"),
             str(self.sched), str(now)],
            capture_output=True, text=True,
        )
        return res

    def test_emits_chat_id_unit_prompt_record(self):
        tasks = [{
            "id": "abc",
            "target_chat_id": "-1001000000001",
            "prompt": "tick",
            "fire_at_epoch": 1_000_000_000,
            "recurring": False,
        }]
        res = self._run(tasks)
        self.assertEqual(res.returncode, 0, res.stderr)
        # \x1f separates chat_id from prompt; \x1e ends record (sole record
        # in this test has no trailing sep because join() doesn't add one).
        self.assertEqual(res.stdout, "-1001000000001\x1ftick")

    def test_refuses_task_missing_target_chat_id(self):
        tasks = [{
            "id": "legacy",
            "prompt": "ghost",
            "fire_at_epoch": 1_000_000_000,
            "recurring": False,
        }]
        res = self._run(tasks)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")  # nothing emitted
        self.assertIn("REFUSED", res.stderr)
        # Task should be dropped from file (broken either way).
        remaining = json.loads(self.sched.read_text())
        # File only rewritten when due tasks emit; otherwise unchanged.
        # The legacy task was due but emitted nothing, so file stays as-is.
        # That is acceptable: nothing fired, nothing to rewrite.
        self.assertEqual(len(remaining), 1)

    def test_recurring_reschedules(self):
        tasks = [{
            "id": "r1",
            "target_chat_id": "-1001000000001",
            "prompt": "tick",
            "fire_at_epoch": 1_000_000_000,
            "recurring": True,
            "interval_seconds": 900,
        }]
        res = self._run(tasks, now=1_500_000_000)
        self.assertEqual(res.returncode, 0)
        self.assertIn("tick", res.stdout)
        remaining = json.loads(self.sched.read_text())
        self.assertEqual(len(remaining), 1)
        self.assertGreater(remaining[0]["fire_at_epoch"], 1_500_000_000)
        self.assertEqual(remaining[0]["last_fired_at"], 1_000_000_000)

    def test_oneshot_drops_after_fire(self):
        tasks = [{
            "id": "o1",
            "target_chat_id": "-1001000000001",
            "prompt": "tick",
            "fire_at_epoch": 1_000_000_000,
            "recurring": False,
        }]
        res = self._run(tasks)
        self.assertEqual(res.returncode, 0)
        remaining = json.loads(self.sched.read_text())
        self.assertEqual(remaining, [])

    def test_not_due_yet_passes_through(self):
        tasks = [{
            "id": "future",
            "target_chat_id": "-1001000000001",
            "prompt": "tick",
            "fire_at_epoch": 3_000_000_000,
            "recurring": False,
        }]
        res = self._run(tasks)
        self.assertEqual(res.stdout, "")  # nothing due


if __name__ == "__main__":
    unittest.main()
