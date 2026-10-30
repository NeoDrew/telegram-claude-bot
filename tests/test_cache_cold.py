#!/usr/bin/env python3
"""The footer says nothing about the cache until it expires; then the last
footer is edited to add "Cache cold · next message ≈ $X", buttons kept.

Runs one turn through the real streamer main() (group chat, edit path),
then ages the recorded cache past its TTL and fires the cold check.

Run: python3 tests/test_cache_cold.py
"""
import importlib.util, io, json, os, sys, tempfile, urllib.parse
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)


def main():
    import urllib.request
    calls, mid = [], [500]

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
    chat = "-100200302"
    os.environ.update(TELEGRAM_BOT_TOKEN="t", CHAT_ID=chat, TELEGRAM_CHAT_ID=chat)
    spec = importlib.util.spec_from_file_location("stream_to_telegram",
                                                  os.path.join(SCRIPTS, "stream_to_telegram.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TELEGRAM_BOT_TOKEN, mod.TELEGRAM_CHAT_ID = "t", chat
    mod.API_URL = "https://api.telegram.org/bott"
    mod.EDIT_INTERVAL = mod.GROUP_EDIT_INTERVAL = 0.0
    mod._log_sent = lambda m: None
    mod.REPO = Path(tempfile.mkdtemp())
    room = mod.REPO / "state" / "rooms" / chat
    room.mkdir(parents=True)
    se = lambda e: {"type": "stream_event", "event": e}
    evs = [se({"type": "message_start"}),
           se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
           se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done."}}),
           se({"type": "content_block_stop", "index": 0}),
           se({"type": "message_stop"}),
           {"type": "result", "result": "ok", "is_error": False, "duration_ms": 4000,
            "usage": {"iterations": [{"input_tokens": 2, "cache_read_input_tokens": 335006,
                                      "cache_creation_input_tokens": 338}],
                      "cache_creation": {"ephemeral_1h_input_tokens": 338}},
            "modelUsage": {"claude-opus-5-5": {"contextWindow": 1000000}}}]
    sys.stdin = io.StringIO("\n".join(json.dumps(e) for e in evs) + "\n")
    mod.main()
    mod._cancel_cold_timer()

    fail = []
    footers = [p for m, p in calls if m == "sendMessage" and "Worked for" in p.get("text", "")]
    if len(footers) != 1 or "ache" in footers[0]["text"]:
        fail.append(f"footer should say nothing about the cache: {footers}")
    st = json.loads((room / ".cache_state").read_text())
    if not st.get("footer_mid"):
        fail.append(f"footer not remembered: {st}")
    st["at"] -= st["ttl"] + 10
    (room / ".cache_state").write_text(json.dumps(st))
    mod._mark_cold(st["at"])
    edits = [p for m, p in calls if m == "editMessageText"
             and p.get("message_id") == str(st.get("footer_mid"))]
    if not edits or "Cache cold · next message ≈ $2.68" not in edits[-1]["text"] \
            or "Worked for" not in edits[-1]["text"] or "act:menu_new" not in edits[-1].get("reply_markup", ""):
        fail.append(f"expired cache should edit the footer, buttons kept: {edits}")
    n = len(edits)
    mod._mark_cold(st["at"])
    if len([1 for m, p in calls if m == "editMessageText"
            and p.get("message_id") == str(st.get("footer_mid"))]) != n:
        fail.append("cold note added twice")
    if fail:
        print("FAILED:\n  " + "\n  ".join(fail)); sys.exit(1)
    print("ok: footer silent until expiry, then marked cold once with its buttons\nall passed")


if __name__ == "__main__":
    main()
