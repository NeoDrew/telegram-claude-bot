#!/usr/bin/env python3
"""
login_watcher.py: phone-driven OAuth token rotation for the bot.

Architecture:

  1. Spawn `claude setup-token` under a pseudo-terminal (it's a full-screen
     TUI; without a PTY it bails in <1s). Force a wide terminal so the URL
     doesn't wrap mid-character.
  2. Read PTY output, strip ANSI, scrape the auth URL that setup-token
     prints as a "Browser didn't open?" fallback. Send the URL to
     Telegram for the user to tap from their phone.
  3. Drop a marker file so the listener intercepts the user's NEXT
     free-text message as the OAuth code (the redirect page shows the
     code after authorization). Listener writes that code to a FIFO we
     own.
  4. Read the code from the FIFO, write it into setup-token's PTY +
     newline. Setup-token exchanges the code with Anthropic, gets the
     long-lived OAuth token (~1 year TTL), stores it to keychain, and
     prints success.
  5. Capture the token from PTY output (preferred) or fall back to
     reading the keychain / .credentials.json.
  6. Atomically rewrite .env, kickstart the listener via launchctl.

Why this design:
  - The bot can't run setup-token from the listener directly because the
    listener gets torn down by the kickstart at the end. So the watcher
    is detached via nohup + setsid + disown.
  - PKCE means we can't roll our own OAuth flow without the
    setup-token-generated verifier. So we drive setup-token instead.
  - Suppressing the local browser auto-open with BROWSER=true mostly
    works but setup-token also prints the URL as a TUI fallback ~3s
    after spawn, we wait for that line either way.

Lifecycle markers under state/:
  .login_in_progress    pid of the watcher; prevents concurrent flows
  .awaiting_oauth_code  exists between URL-sent and code-received; the
                        listener treats the next text as the code
  .oauth_code.fifo      named pipe; listener writes the code, watcher
                        reads it. Recreated each run.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_config  # noqa: E402

# --------------------------------------------------------------------------
# Paths + env

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / "state"
LOGS = REPO_ROOT / "logs"
STATE.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

LOCK = STATE / ".login_in_progress"
MARKER = STATE / ".awaiting_oauth_code"
# Regular file (not FIFO) for the listener → watcher code handoff.
# FIFOs race when the writer opens before the reader has it open:
# data lands but the writer can complete its blocking write only
# after the reader is also attached. Polling a plain file is dumber
# but eliminates the race entirely.
CODE_FILE = STATE / ".oauth_code"
ENV_FILE = REPO_ROOT / ".env"
LOG_PATH = LOGS / "login.log"
PTY_LOG = LOGS / "login_pty.log"  # raw + cleaned PTY transcript
TG = str(REPO_ROOT / "scripts" / "tg.sh")
BACK_KB = '[[{"text":"⬅ Menu","callback_data":"act:menu"}]]'
TIMEOUT_SEC = 600  # 10 minutes for the user to tap link + paste code


# --------------------------------------------------------------------------
# Helpers

def bot_label() -> str:
    """launchd label of the listener job (see lib.sh's bot_label)."""
    return bot_config.get("BOT_LAUNCHD_LABEL", "com.telegram-claude-bot")


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except Exception:
        pass


def tg_send(text: str, kb: str = BACK_KB) -> None:
    """Send a Telegram message via tg.sh kb (keyboard preserved)."""
    env = os.environ.copy()
    env["CHAT_ID"] = CHAT_ID
    try:
        subprocess.run([TG, "kb", text, kb], env=env, timeout=15, check=False,
                       capture_output=True)
    except Exception as e:
        log(f"tg send failed: {e}")


ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\r"
)
URL_RE = re.compile(rb"https://claude\.com/cai/oauth/authorize\?[A-Za-z0-9._~:/?#%&=+-]+")
TOKEN_RE = re.compile(rb"sk-ant-oat0[12]-[A-Za-z0-9_-]{40,}")


def cleanup() -> None:
    """Remove all per-flow markers and the code file. Idempotent."""
    for p in (LOCK, MARKER, CODE_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"cleanup unlink {p}: {e}")


# --------------------------------------------------------------------------
# Argument parsing + concurrency lock

if len(sys.argv) < 2 or not sys.argv[1]:
    sys.stderr.write("usage: login_watcher.py <chat_id>\n")
    sys.exit(2)
CHAT_ID = sys.argv[1]

# Refuse to start if another watcher is already running for any room.
# claude setup-token + the keychain are shared per-user, so concurrent
# flows would step on each other.
if LOCK.exists():
    try:
        existing_pid = int(LOCK.read_text().strip())
        os.kill(existing_pid, 0)  # raises if dead
        log(f"another watcher alive (pid={existing_pid}); aborting")
        tg_send(f"🔐 Login already in progress (pid {existing_pid}). Wait or retry later.")
        sys.exit(0)
    except (ValueError, ProcessLookupError, OSError):
        # Stale lock: wipe and continue.
        try:
            LOCK.unlink()
        except FileNotFoundError:
            pass

LOCK.write_text(str(os.getpid()))
atexit.register(cleanup)

# Catch SIGTERM cleanly so launchctl restart at the end doesn't leave
# the marker behind.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# --------------------------------------------------------------------------
# Locate the claude binary

CLAUDE_BIN = os.path.expanduser(bot_config.get("CLAUDE_BIN_OVERRIDE", "~/.local/bin/claude"))
if not (os.path.isfile(CLAUDE_BIN) and os.access(CLAUDE_BIN, os.X_OK)):
    import shutil
    CLAUDE_BIN = shutil.which("claude") or ""
if not CLAUDE_BIN:
    tg_send("❌ claude binary not found on the bot host. Login can't run.")
    log("claude binary missing")
    sys.exit(1)

log(f"==== watcher start chat={CHAT_ID} pid={os.getpid()} bin={CLAUDE_BIN} ====")


# --------------------------------------------------------------------------
# Clear any stale code file from a prior run so we don't immediately
# consume an orphan value.

try:
    CODE_FILE.unlink()
except FileNotFoundError:
    pass

# Truncate the PTY transcript log; we want a clean dump per flow so
# debugging failures doesn't require sifting through old runs.
PTY_LOG.write_bytes(b"")


# --------------------------------------------------------------------------
# Spawn claude setup-token under a wide PTY.

child_pid, master_fd = pty.fork()
if child_pid == 0:
    # In the child. Scrub the bot's stale auth env so setup-token starts
    # a clean OAuth flow; otherwise it might detect an existing session
    # and behave differently. Force BROWSER=true so the auto-open is a
    # no-op (the URL still appears in the TUI fallback).
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_BASE_URL", "HTTPS_PROXY", "HTTP_PROXY"):
        os.environ.pop(k, None)
    os.environ["BROWSER"] = "true"
    os.environ["TERM"] = "xterm-256color"
    os.execvp(CLAUDE_BIN, ["claude", "setup-token"])
    # unreachable
    os._exit(127)

# In the parent. Set a wide window so the URL line doesn't wrap and
# break the regex match.
fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
            struct.pack("HHHH", 50, 400, 0, 0))
log(f"spawned setup-token child pid={child_pid} master_fd={master_fd}")


# --------------------------------------------------------------------------
# Main loop: pump PTY output until we either see the URL → token, the
# child exits, or the timeout expires.

buf = b""
url_sent = False
code_forwarded = False
got_token: str | None = None
deadline = time.time() + TIMEOUT_SEC


def drain_pty(max_bytes: int = 65536) -> bytes:
    """Non-blocking drain of whatever's currently in the PTY buffer."""
    out = b""
    while True:
        r, _, _ = select.select([master_fd], [], [], 0)
        if not r:
            break
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if len(out) >= max_bytes:
            break
    return out


def read_code_file() -> bytes | None:
    """Read + delete state/.oauth_code if present. None if empty/missing.

    Plain file with atomic consume: read the bytes, then unlink so we
    don't re-consume on the next loop iteration.
    """
    try:
        data = CODE_FILE.read_bytes()
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"code file read error: {e}")
        return None
    try:
        CODE_FILE.unlink()
    except FileNotFoundError:
        pass
    return data or None


def dump_pty(chunk: bytes) -> None:
    """Append raw PTY bytes to the transcript log (for debugging)."""
    try:
        with open(PTY_LOG, "ab") as f:
            f.write(chunk)
    except Exception:
        pass


while time.time() < deadline:
    # 1. Pump PTY output.
    r, _, _ = select.select([master_fd], [], [], 0.4)
    if r:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            data = b""
        if not data:
            # Read returned nothing: child probably exited. Confirm
            # below and break.
            pass
        else:
            buf += data
            dump_pty(data)

    clean = ANSI_RE.sub(b"", buf)

    # 2. URL discovery (one-shot).
    if not url_sent:
        m = URL_RE.search(clean)
        if m:
            url = m.group(0).decode("ascii", errors="replace")
            # The TUI may have inserted newlines in the URL when the
            # terminal width was narrower than the URL length. Our wide
            # PTY mostly prevents this but strip lingering whitespace
            # just in case.
            url = re.sub(r"\s+", "", url)
            log(f"captured URL len={len(url)}")
            MARKER.touch()  # signal listener to capture next text
            tg_send(
                "🔐 *Tap to authorize the bot*\n\n"
                f"{url}\n\n"
                "After you approve in your browser, Anthropic shows you "
                "a *code*. Copy that code and *paste it back here as a "
                "normal message*. The bot will exchange it for a fresh "
                "long-lived OAuth token, write `.env`, and restart "
                "itself.\n\n"
                "_Times out in 10 min; tap *🔐 login* again if you miss "
                "the window._"
            )
            url_sent = True

    # 3. Look for the token (post-exchange success).
    m = TOKEN_RE.search(clean)
    if m:
        got_token = m.group(0).decode("ascii", errors="replace")
        log(f"captured token in stream len={len(got_token)}")
        break

    # 4. Drain anything the listener wrote to the code file.
    if url_sent and not code_forwarded:
        payload = read_code_file()
        if payload:
            code = payload.decode("utf-8", errors="replace").strip()
            if code:
                log(f"received code from listener len={len(code)}")
                try:
                    MARKER.unlink()
                except FileNotFoundError:
                    pass
                # Setup-token's TUI has bracketed-paste mode enabled
                # (we observed \e[?2004h in the PTY transcript). The
                # input prompt only treats large input as a single
                # "paste" when it's wrapped in the bracketed-paste
                # delimiters \e[200~ ... \e[201~. Without them, the
                # readline buffer sees the chars as individual
                # keypresses and silently ignores most of the payload.
                # After the wrapped paste we send \r to submit.
                try:
                    payload = (b"\x1b[200~" + code.encode("utf-8")
                               + b"\x1b[201~\r")
                    os.write(master_fd, payload)
                    log(f"wrote bracketed-paste code to PTY (len={len(code)})")
                except OSError as e:
                    log(f"PTY write failed: {e}")
                code_forwarded = True
                tg_send("⏳ Exchanging the code with Anthropic…")

    # 5. Has the child exited?
    try:
        wpid, status = os.waitpid(child_pid, os.WNOHANG)
        if wpid == child_pid:
            log(f"child exited status={status}")
            # Drain any final output before declaring victory or defeat.
            final = drain_pty()
            if final:
                buf += final
                dump_pty(final)
            clean = ANSI_RE.sub(b"", buf)
            m = TOKEN_RE.search(clean)
            if m:
                got_token = m.group(0).decode("ascii", errors="replace")
                log(f"captured token post-exit len={len(got_token)}")
            break
    except ChildProcessError:
        break


# Kill any lingering subprocess (safety net for timeouts).
try:
    os.kill(child_pid, 9)
except ProcessLookupError:
    pass


# --------------------------------------------------------------------------
# Fallback token discovery: keychain + .credentials.json.
#
# If `claude setup-token` succeeded but didn't echo the token to stdout,
# we can still read it back. The token is stored either in macOS Keychain
# under "Claude Code-credentials" or in ~/.claude/.credentials.json.

def find_token_in_obj(obj):
    """Recursive walk: find any string starting with sk-ant-oat0..."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, str) and o.startswith("sk-ant-oat0"):
            return o
        if isinstance(o, dict):
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


if not got_token:
    cred = Path.home() / ".claude" / ".credentials.json"
    if cred.exists():
        try:
            t = find_token_in_obj(json.loads(cred.read_text()))
            if t:
                got_token = t
                log("token from ~/.claude/.credentials.json")
        except Exception as e:
            log(f".credentials.json read failed: {e}")

if not got_token:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s",
             "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            blob = out.stdout.strip()
            try:
                t = find_token_in_obj(json.loads(blob))
            except Exception:
                t = blob if blob.startswith("sk-ant-oat0") else None
            if t:
                got_token = t
                log("token from keychain")
    except Exception as e:
        log(f"keychain read failed: {e}")


# --------------------------------------------------------------------------
# Outcome: success → rotate + restart, failure → tell the user what
# setup-token actually said.

if not got_token:
    tail = ANSI_RE.sub(b"", buf).decode("utf-8", errors="replace")[-600:]
    tail = tail.strip() or "(no output)"
    tg_send(
        "❌ Login flow ended without a token.\n\n"
        "Last output from `claude setup-token`:\n"
        f"```\n{tail}\n```\n\n"
        "Tap *🔐 login* on the menu to retry."
    )
    log("no token captured; flow failed")
    sys.exit(0)


# Atomic .env rewrite (tempfile → fsync → rename).
src = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
out_lines: list[str] = []
seen = False
for line in src:
    if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
        out_lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={got_token}")
        seen = True
    else:
        out_lines.append(line)
if not seen:
    out_lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={got_token}")

tmp_fd, tmp_path = tempfile.mkstemp(dir=str(ENV_FILE.parent), prefix=".env.")
with os.fdopen(tmp_fd, "w") as f:
    f.write("\n".join(out_lines) + "\n")
    f.flush()
    os.fsync(f.fileno())
os.chmod(tmp_path, 0o600)
os.replace(tmp_path, str(ENV_FILE))
log(f"wrote token to {ENV_FILE} (len={len(got_token)})")

tg_send("✅ Token rotated. Bot restarting…")
time.sleep(1)

uid = os.getuid()
subprocess.run(
    ["launchctl", "kickstart", "-k",
     f"gui/{uid}/{bot_label()}"],
    capture_output=True, timeout=10,
)
log("kickstart issued")
