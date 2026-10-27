#!/usr/bin/env python3
"""
harness.py - orchestrates a full, isolated run of the real telegram-claude-bot
pipeline (listener.sh -> spawn_claude.sh -> stream_to_telegram.py) against
two fakes instead of the real Telegram Bot API and the real claude CLI:

  - fakes/fake_telegram_server.py stands in for api.telegram.org.
  - fakes/fake_claude.py stands in for the claude binary, driven by a small
    JSON "script" fixture describing what each turn should emit (see that
    file's docstring).

Why a sandbox copy: REPO_ROOT in every script here is computed as
dirname(BASH_SOURCE)/.. at runtime, so copying scripts/ + hooks/ into a temp
dir and running listener.sh from there gets a fully isolated state/ and
logs/ tree for free, with zero changes to the scripts' own path logic. The
only seams needed are env vars the real code already reads: TELEGRAM_API_BASE
(scripts/listener.sh, tg.sh, stream_to_telegram.py) and CLAUDE_BIN_OVERRIDE
(scripts/spawn_claude.sh).

Not a general test framework: this is deliberately small and specific to
this repo's pipeline. See tests/e2e/README.md for how to write a new
scenario against it.
"""

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
E2E_DIR = os.path.join(REPO_ROOT, "tests", "e2e")
# Multiplies every wait below. run.sh sets 2 when scenarios run in parallel,
# where the extra load makes the real listener noticeably slower to react.
TIMEOUT_SCALE = float(os.environ.get("E2E_TIMEOUT_SCALE", "1"))
FAKES_DIR = os.path.join(E2E_DIR, "fakes")

OWNER_USER_ID = "555000111"   # >= 1_000_000 so lib.sh::is_valid_chat_id accepts it
DM_CHAT_ID = OWNER_USER_ID    # Telegram DMs: chat_id == the user's own id


class TimeoutWaitingForCall(AssertionError):
    pass


class E2EHarness:
    def __init__(self, label="e2e", keep_sandbox=None):
        self.label = label
        self.keep_sandbox = (
            keep_sandbox if keep_sandbox is not None
            else bool(os.environ.get("KEEP_SANDBOX")))
        self.sandbox = None
        self.tg_port = None
        self.tg_proc = None
        self.listener_proc = None
        self._torn_down = False
        self._prev_sigterm = None
        self._prev_sigint = None

    # ---- lifecycle ---------------------------------------------------

    def setup(self, fake_claude_script=None, extra_env=None):
        self.sandbox = tempfile.mkdtemp(prefix=f"tcbot-e2e-{self.label}-")
        for sub in ("scripts", "hooks"):
            src = os.path.join(REPO_ROOT, sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(self.sandbox, sub))

        env_path = os.path.join(self.sandbox, ".env")
        with open(env_path, "w") as f:
            f.write(f"TELEGRAM_BOT_TOKEN=fake-token-{self.label}\n")
            f.write(f"TELEGRAM_CHAT_ID={OWNER_USER_ID}\n")
            f.write("CLAUDE_CODE_OAUTH_TOKEN=fake-oauth-token-unused\n")

        self._start_fake_telegram()

        fake_claude_bin = os.path.join(FAKES_DIR, "fake_claude.py")
        self.env = dict(os.environ)
        self.env.update({
            "TELEGRAM_API_BASE": f"http://127.0.0.1:{self.tg_port}",
            "CLAUDE_BIN_OVERRIDE": fake_claude_bin,
            "CLAUDE_HOOKS_SUPPRESS_TELEGRAM": "1",
            # SAFETY DEFAULT, do not remove: BOT_RESTART_CMD's real default
            # is scripts/safe_restart.sh --force, which shells out to the
            # REAL system launchctl to kickstart the REAL production
            # listener job (BOT_LAUNCHD_LABEL); this sandbox
            # has no launchd registration of its own, so that command would
            # reach past the sandbox entirely. No e2e-tested code path used
            # to be able to trigger a restart on its own (test_multi_room_
            # restart_survival.py restarts the sandboxed subprocess directly
            # from Python, never through this env var), but
            # listener.sh's own auto-restart-on-stale-code check
            # (maybe_auto_restart) now can, from ANY test, the
            # moment its staleness check ever produces a false positive
            # against a freshly-copied sandbox. Defaulting to a no-op here
            # closes that off for every test by default; a test that
            # specifically wants to verify the trigger itself fires can
            # still override both via its own extra_env (see
            # test_dispatch.sh's identical BOT_RESTART_CMD=true pattern for
            # the manual /restart path).
            "BOT_RESTART_CMD": "true",
            "BOT_RESTART_DELAY": "0",
        })
        if fake_claude_script:
            self.env["FAKE_CLAUDE_SCRIPT"] = fake_claude_script
        if extra_env:
            self.env.update(extra_env)

        # Guarantee cleanup even when this process is killed from the
        # outside (an outer `timeout N python3 test_x.py` sending SIGTERM,
        # or Ctrl-C) rather than exiting through the test's own try/finally.
        # Without this, any turn still in flight when that happens (a
        # scripted {"hang": true}, or just a slow one) leaks its whole
        # tail/claude/streamer/diagnostics process tree, since nothing is
        # left to reap it (see teardown()'s docstring for why _kill_listener
        # alone can't reach it either). Previous handlers are restored in
        # teardown() so sequential harnesses in one process (multi-case
        # test files) don't stack handlers referencing a torn-down instance.
        self._prev_sigterm = signal.signal(signal.SIGTERM, self._signal_teardown)
        self._prev_sigint = signal.signal(signal.SIGINT, self._signal_teardown)
        atexit.register(self.teardown)
        return self

    def _signal_teardown(self, signum, frame):
        self.teardown()
        # Re-raise the signal's default disposition so the process actually
        # exits instead of silently surviving its own SIGTERM/SIGINT.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _start_fake_telegram(self):
        server_path = os.path.join(FAKES_DIR, "fake_telegram_server.py")
        self.tg_proc = subprocess.Popen(
            [sys.executable, server_path, "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        port_line = self.tg_proc.stdout.readline().strip()
        if not port_line.isdigit():
            raise RuntimeError(
                f"fake_telegram_server.py did not print a port: {port_line!r}")
        self.tg_port = int(port_line)
        # Drain the pipe in the background so the server's own stdout
        # buffer never fills and blocks it.
        self._tg_log = os.path.join(self.sandbox, "logs", "fake_telegram.log")
        os.makedirs(os.path.dirname(self._tg_log), exist_ok=True)

        def _drain():
            with open(self._tg_log, "a") as f:
                for line in self.tg_proc.stdout:
                    f.write(line)
        import threading
        threading.Thread(target=_drain, daemon=True).start()

    def start_listener(self):
        listener_path = os.path.join(self.sandbox, "scripts", "listener.sh")
        log_dir = os.path.join(self.sandbox, "logs")
        os.makedirs(log_dir, exist_ok=True)
        out_path = os.path.join(log_dir, "listener_stdio.log")
        with open(out_path, "w") as out:
            self.listener_proc = subprocess.Popen(
                ["/bin/bash", listener_path],
                cwd=self.sandbox, env=self.env,
                stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True)
        # Wait for the boot self-test's getMe call to land, proving the
        # listener is up and pointed at our fake server (not hanging on a
        # missing .env var or crashing before the main loop).
        self._wait_for_call("getMe", lambda p: True, timeout=15)
        return self

    def _kill_listener(self):
        if not (self.listener_proc and self.listener_proc.poll() is None):
            return
        try:
            os.killpg(os.getpgid(self.listener_proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.listener_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.listener_proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def restart_listener(self):
        """Kill the listener process and launch a fresh one against the
        SAME sandbox (state/, .env, everything on disk untouched) - mirrors
        a real ♻️ restart / launchd kickstart. spawn_claude.sh detaches via
        setsid into its own process group (see "own pgid via setsid" in
        spawn.log), so an in-flight spawn in another room survives this;
        only the listener process itself is replaced."""
        self._kill_listener()
        self.start_listener()
        return self

    def _reap_sandbox_processes(self):
        """Safety-net kill for anything still referencing this sandbox's
        path, independent of process-group tracking.

        _kill_listener() only killpg's the LISTENER's own process group,
        but scripts/with_setsid.py calls os.setsid() before exec'ing
        spawn_claude.sh, putting it in a BRAND NEW session/pgid, deliberately
        detached from the listener's. That's the exact mechanism that lets a
        real spawn survive a listener restart in the running bot, but it means
        _kill_listener() structurally cannot reach a spawn_claude.sh (or its
        tail/streamer/spawn_diagnostics.sh children) that's still alive when
        a test ends: a scripted {"hang": true} turn, or the test process
        itself dying before it gets to cancel one. Every process in this
        pipeline is launched with sandbox-relative paths as arguments
        (STREAM_LOG, ROOM_DIR, --mcp-config, etc.), so matching on the
        sandbox's own path via `pgrep -f` catches the whole tree regardless
        of which process group it landed in.
        """
        if not self.sandbox:
            return

        def _matching_pids():
            try:
                out = subprocess.run(
                    ["pgrep", "-f", self.sandbox],
                    capture_output=True, text=True, timeout=5).stdout
            except Exception:
                return []
            return [p for p in out.split() if p.isdigit()]

        def _kill_all(pids, sig):
            for pid in pids:
                try:
                    os.kill(int(pid), sig)
                except OSError:
                    # ProcessLookupError (already gone) or a transient
                    # permission/errno hiccup; either way, don't let one
                    # bad pid abort the rest of the sweep.
                    pass

        pids = _matching_pids()
        if not pids:
            return
        _kill_all(pids, signal.SIGTERM)
        # Escalating retries, not a single SIGTERM-then-SIGKILL pass: a
        # python child (fake_claude.py) doesn't always act on SIGTERM
        # within one short sleep, and a single follow-up check can race it.
        # SIGKILL can't be blocked, so once we're sending that nothing
        # should survive three rounds baring a genuinely stuck (D-state)
        # process, which no signal can touch anyway.
        for wait_s, sig in ((0.3, signal.SIGKILL),
                            (0.5, signal.SIGKILL),
                            (1.0, signal.SIGKILL)):
            time.sleep(wait_s)
            remaining = _matching_pids()
            if not remaining:
                return
            _kill_all(remaining, sig)
        survivors = _matching_pids()
        if survivors:
            print(f"[{self.label}] WARNING: {len(survivors)} process(es) "
                  f"survived SIGKILL and still reference {self.sandbox}: "
                  f"{survivors}", file=sys.stderr)

    def teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        # Restore whatever handler setup() displaced, so a second harness
        # created later in the SAME process (multi-case test files each
        # make their own E2EHarness) doesn't inherit a signal handler bound
        # to this now-torn-down instance.
        if self._prev_sigterm is not None:
            signal.signal(signal.SIGTERM, self._prev_sigterm)
        if self._prev_sigint is not None:
            signal.signal(signal.SIGINT, self._prev_sigint)
        try:
            atexit.unregister(self.teardown)
        except Exception:
            pass

        self._kill_listener()
        self._reap_sandbox_processes()
        if self.tg_proc and self.tg_proc.poll() is None:
            self.tg_proc.terminate()
            try:
                self.tg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tg_proc.kill()
        if self.sandbox and not self.keep_sandbox:
            shutil.rmtree(self.sandbox, ignore_errors=True)
        elif self.sandbox:
            print(f"[{self.label}] sandbox kept at: {self.sandbox}", file=sys.stderr)

    # ---- fake Telegram control ----------------------------------------

    def _tg_url(self, path):
        return f"http://127.0.0.1:{self.tg_port}{path}"

    def _post_json(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self._tg_url(path), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _get_json(self, path):
        with urllib.request.urlopen(self._tg_url(path), timeout=10) as r:
            return json.loads(r.read())

    def inject_dm(self, text, user_id=OWNER_USER_ID, first_name="Alice",
                  message_id=None):
        return self._inject_message(
            chat_id=int(user_id), user_id=int(user_id),
            first_name=first_name, text=text, message_id=message_id)

    def inject_group_message(self, chat_id, text, user_id=OWNER_USER_ID,
                              first_name="Alice", message_id=None):
        return self._inject_message(
            chat_id=int(chat_id), user_id=int(user_id),
            first_name=first_name, text=text, message_id=message_id)

    def _inject_message(self, chat_id, user_id, first_name, text, message_id):
        mid = message_id or int(time.time() * 1000) % 1_000_000
        update = {
            "message": {
                "message_id": mid,
                "date": int(time.time()),
                "chat": {"id": chat_id,
                         "type": "private" if chat_id > 0 else "group"},
                "from": {"id": user_id, "first_name": first_name,
                         "is_bot": False},
                "text": text,
            }
        }
        return self._post_json("/__test__/inject_update", update)

    def inject_callback_query(self, data, chat_id, message_id=1,
                               user_id=OWNER_USER_ID, first_name="Alice",
                               cb_id=None):
        """Simulate an inline-keyboard button tap. `data` is the raw
        callback_data string, e.g. "act:cancel yes" or "act:menu"."""
        cb_id = cb_id or f"cbq{int(time.time() * 1000) % 10_000_000}"
        update = {"callback_query": {
            "id": cb_id,
            "from": {"id": int(user_id), "first_name": first_name,
                     "is_bot": False},
            "message": {"message_id": message_id,
                        "chat": {"id": int(chat_id)}},
            "data": data,
        }}
        return self._post_json("/__test__/inject_update", update)

    def set_chat_member(self, chat_id, status="member", can_change_info=False):
        self._post_json("/__test__/set_chat_member", {
            "chat_id": str(chat_id), "status": status,
            "can_change_info": can_change_info})

    def all_calls(self, method=None):
        calls = self._get_json("/__test__/calls")["calls"]
        if method:
            calls = [c for c in calls if c["method"] == method]
        return calls

    def _wait_for_call(self, method, predicate, timeout=20, poll=0.2):
        deadline = time.time() + timeout * TIMEOUT_SCALE
        last_err = None
        while time.time() < deadline:
            try:
                calls = self.all_calls(method)
            except (urllib.error.URLError, ConnectionError) as e:
                last_err = e
                time.sleep(poll)
                continue
            for c in calls:
                try:
                    if predicate(c["params"]):
                        return c
                except Exception:
                    continue
            time.sleep(poll)
        raise TimeoutWaitingForCall(
            f"[{self.label}] timed out after {timeout}s waiting for "
            f"{method} matching predicate. Recent calls:\n"
            + self._dump_calls() + self._dump_logs())

    def wait_for_call(self, method, predicate, timeout=20):
        return self._wait_for_call(method, predicate, timeout=timeout)

    def _dump_calls(self, n=20):
        try:
            calls = self.all_calls()[-n:]
        except Exception as e:
            return f"  (could not fetch calls: {e})\n"
        lines = []
        for c in calls:
            text = c["params"].get("text", "")
            lines.append(f"  {c['method']}: {text[:120]!r}")
        return "\n".join(lines) + "\n"

    def _dump_logs(self, n=40):
        out = []
        for name in ("spawn.log", "listener.log"):
            path = os.path.join(self.sandbox, "logs", name)
            if not os.path.exists(path):
                continue
            with open(path) as f:
                tail = f.readlines()[-n:]
            out.append(f"--- {name} (last {len(tail)} lines) ---\n" + "".join(tail))
        return "\n".join(out)

    # ---- room state readers --------------------------------------------

    def room_dir(self, chat_id):
        return os.path.join(self.sandbox, "state", "rooms", str(chat_id))

    def read_room_file(self, chat_id, name, default=""):
        path = os.path.join(self.room_dir(chat_id), name)
        if not os.path.exists(path):
            return default
        with open(path) as f:
            return f.read()

    def read_room_json(self, chat_id, name, default=None):
        raw = self.read_room_file(chat_id, name, default="")
        if not raw.strip():
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def session_id(self, chat_id):
        data = self.read_room_json(chat_id, "sessions.json", default={})
        return (data or {}).get("session_id")

    def wait_for_room_file(self, chat_id, name, predicate=lambda s: True,
                            timeout=15, poll=0.2):
        deadline = time.time() + timeout * TIMEOUT_SCALE
        last = None
        while time.time() < deadline:
            last = self.read_room_file(chat_id, name, default=None)
            if last is not None and predicate(last):
                return last
            time.sleep(poll)
        raise TimeoutWaitingForCall(
            f"[{self.label}] timed out after {timeout}s waiting for "
            f"state/rooms/{chat_id}/{name} to satisfy predicate. "
            f"Last seen: {last!r}\n" + self._dump_calls() + self._dump_logs())

    def seed_room_file(self, chat_id, name, content):
        """Write a per-room state file directly (e.g. mentions_only,
        effort, model) before or between messages, bypassing the real
        menu-toggle handler for tests that only care about the resulting
        gate/behaviour, not the toggle UI itself."""
        rd = self.room_dir(chat_id)
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, name), "w") as f:
            f.write(content)

    def approved_users(self, chat_id):
        data = self.read_json(os.path.join(
            self.sandbox, "state", "approved_users.json"), default={})
        return data.get(str(chat_id), [])

    def pending_users(self, chat_id):
        data = self.read_json(os.path.join(
            self.sandbox, "state", "pending_users.json"), default={})
        return data.get(str(chat_id), [])

    def read_json(self, abs_path, default=None):
        if not os.path.exists(abs_path):
            return default
        try:
            with open(abs_path) as f:
                return json.load(f)
        except Exception:
            return default

    def seed_approved_user(self, chat_id, user_id, username=""):
        """Pre-seed approved_users.json directly (bypasses the real
        contact-picker / pending-approve UI flow, which this harness does
        not simulate) so a test can assert dispatch behaviour for an
        approved non-owner user without also testing the approval UI."""
        path = os.path.join(self.sandbox, "state", "approved_users.json")
        data = self.read_json(path, default={})
        data.setdefault(str(chat_id), []).append({
            "id": int(user_id), "username": username,
            "approved_at": int(time.time())})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)


def write_fake_claude_script(path, turns):
    with open(path, "w") as f:
        json.dump({"turns": turns}, f)
    return path


def write_fake_claude_rooms_script(path, rooms, default_turns=None):
    """rooms: {chat_id (str or int): [turn, turn, ...]}. Lets one fixture
    drive a multi-room scenario where each room gets its own scripted
    conversation independently of what other rooms are doing."""
    doc = {"rooms": {str(cid): {"turns": turns}
                      for cid, turns in rooms.items()}}
    if default_turns is not None:
        doc["turns"] = default_turns
    with open(path, "w") as f:
        json.dump(doc, f)
    return path
