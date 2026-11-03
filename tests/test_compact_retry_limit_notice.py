#!/usr/bin/env python3
"""Two chat bugs, run against the real streamer module:

1. A tool added to a compact list during a Telegram 429 cooldown has its edit
   skipped. It must still appear once the cooldown ends, even with no later
   tool to carry it (a Bash, then an Agent, then the reply: the Agent never
   showed). Driven directly, since a worker that lags sends both at once.
2. A usage limit reaches the chat once. Claude Code sends the limit line as a
   synthetic assistant message before the error result; that must not be
   shown as a reply on top of the bot's "⏸ Rate limited" notice.

Run: python3 tests/test_compact_retry_limit_notice.py
"""
import importlib.util, io, json, os, sys, tempfile, urllib.parse
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)
se = lambda e: {"type": "stream_event", "event": e}


def run(events, fail_edit=None, drive=None):
    import urllib.request
    calls, mid = [], [700]

    class R:
        def __init__(self, b): self.b = b
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.b

    def fake(req, timeout=None):
        m = req.full_url.rsplit("/", 1)[-1]
        p = dict(urllib.parse.parse_qsl(req.data.decode() if req.data else ""))
        calls.append((m, p))
        if m == "sendMessage":
            mid[0] += 1
            return R(json.dumps({"ok": True, "result": {"message_id": mid[0]}}).encode())
        return R(json.dumps({"ok": True, "result": True}).encode())

    urllib.request.urlopen = fake
    chat = "-100200303"
    os.environ.update(TELEGRAM_BOT_TOKEN="t", CHAT_ID=chat, TELEGRAM_CHAT_ID=chat)
    spec = importlib.util.spec_from_file_location(
        "stream_to_telegram", os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN, mod.TELEGRAM_CHAT_ID = "t", chat
    mod.API_URL = "https://api.telegram.org/bott"
    mod.EDIT_INTERVAL = mod.GROUP_EDIT_INTERVAL = 0.0
    mod._log_sent = lambda m: None
    mod.REPO = Path(tempfile.mkdtemp())
    (mod.REPO / "state" / "rooms" / chat).mkdir(parents=True)
    (mod.REPO / "logs").mkdir()
    v = {"tool_calls": "compact", "edits": "compact", "thinking": "off"}
    mod._load_verbosity = lambda: dict(v)
    mod._verbosity = dict(v)
    if fail_edit:
        real, failed = mod.tg_edit, []

        def flaky(m, text):   # a cooldown: tg_api skips the call
            if fail_edit in text and not failed:
                failed.append(text)
                return False
            return real(m, text)
        mod.tg_edit = flaky
        mod._wait_for_cooldown = lambda *a, **k: None
    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in events) + "\n")
    try:
        mod.main()
    except SystemExit:
        pass
    if drive:
        drive(mod)
    mod._marker_queue.join()
    return calls


def main():
    fail = []
    def drive(mod):   # one step at a time, so the second tool is a real edit
        mod._compact_add("⚙️ Bash")
        mod._marker_queue.join()
        mod._compact_add("🤖 Agent")
        mod._marker_queue.join()
        mod._compact_close()
    calls = run([], fail_edit="Agent", drive=drive)
    edits = [p["text"] for m, p in calls if m == "editMessageText"]
    if "⚙️ Bash, 🤖 Agent" in edits:
        print("ok: a tool whose edit fell in a cooldown still appears")
    else:
        fail.append(f"Agent never reached the chat: {edits}")

    banner = "You've hit your weekly limit · resets 7pm (Europe/London)"
    calls = run([
        {"type": "assistant", "error": "rate_limit",
         "message": {"model": "<synthetic>", "role": "assistant",
                     "content": [{"type": "text", "text": banner}]}},
        {"type": "result", "subtype": "success", "is_error": True, "result": banner,
         "api_error_status": 429, "duration_ms": 100}])
    texts = [p.get("text", "") for m, p in calls if m in ("sendMessage", "editMessageText")]
    if any("hit your weekly" in t for t in texts):
        fail.append(f"limit banner shown as a reply: {texts}")
    elif sum("Rate limited" in t for t in texts) != 1:
        fail.append(f"expected one Rate limited notice: {texts}")
    else:
        print("ok: one notice for a usage limit, not two")

    if fail:
        print("FAILED:\n  " + "\n  ".join(fail))
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
