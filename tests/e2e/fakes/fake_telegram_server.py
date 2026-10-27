#!/usr/bin/env python3
"""
fake_telegram_server.py - a stand-in for api.telegram.org, for E2E tests.

Runs a real local HTTP server implementing the subset of the Bot API this
repo's scripts call (listener.sh's getUpdates/getMe/getChat, tg.sh's
send/edit/delete/getChatMember/etc, stream_to_telegram.py's sendMessage/
editMessageText). Point the real pipeline at it via TELEGRAM_API_BASE
(scripts/listener.sh, scripts/tg.sh, scripts/stream_to_telegram.py all
honour that env var; see tests/e2e/README.md).

This is deliberately a real subprocess bound to a real TCP port, not an
in-process mock, because the system under test is a tree of real bash/python
subprocesses (listener.sh -> spawn_claude.sh -> stream_to_telegram.py) that
each independently shell out to curl / urllib. A Python mock object can't
intercept those; an actual server on 127.0.0.1 can.

Every call is recorded (method + params + timestamp) and readable via
/__test__/calls. Incoming Telegram updates (a user "sending a message") are
injected via /__test__/inject_update instead of coming from a real phone.

Usage (as a subprocess): python3 fake_telegram_server.py [--port N]
Prints the bound port as the first stdout line, then serves forever until
killed. Port 0 (default) means "pick a free port".

Test-control endpoints (not real Telegram API paths):
  POST /__test__/inject_update       body = a Telegram Update dict (JSON)
  GET  /__test__/calls               -> {"calls": [...]}
  POST /__test__/reset                clears calls + pending updates
  POST /__test__/set_chat_member     body = {"chat_id","status","can_change_info"}
  POST /__test__/set_override        body = {"method","body","code"} - force the
                                      next N calls (or all, until cleared) to
                                      <method> to return <body>/<code> instead
                                      of the default canned response. Used to
                                      simulate Telegram-side failures (429s,
                                      malformed responses) independent of the
                                      fake claude binary's own failure modes.
  POST /__test__/clear_override      body = {"method"}
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, parse_qsl


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.pending_updates = []
        self.next_update_id = 1000
        self.next_message_id = 500
        self.chat_members = {}
        self.overrides = {}
        self.bot_user = {
            "id": 987654321,
            "is_bot": True,
            "first_name": "FakeAssistant",
            "username": "fakeassistant_bot",
        }


STATE = State()


def record(method, params):
    with STATE.lock:
        STATE.calls.append(
            {"method": method, "params": params, "ts": time.time()})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep test output clean; the /__test__/calls log is the record

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "") or ""
        if "application/json" in ctype:
            try:
                return json.loads(raw or b"{}")
            except Exception:
                return {}
        if not raw:
            return {}
        return dict(parse_qsl(raw.decode(errors="replace")))

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = self._read_body() if self.command == "POST" else {}
        params = {**qs, **body}

        if path.startswith("/__test__/"):
            return self._handle_test(path, params)

        parts = path.strip("/").split("/")
        # File downloads: /file/bot<token>/<file_path>. Serves deterministic
        # bytes derived from the path so a test can check what got saved.
        if len(parts) >= 3 and parts[0] == "file" and parts[1].startswith("bot"):
            record("__file__", {"file_path": "/".join(parts[2:])})
            body = ("FAKEFILE:" + "/".join(parts[2:])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if len(parts) != 2 or not parts[0].startswith("bot"):
            return self._send_json({"ok": False, "description": "not found"}, 404)
        return self._handle_bot_method(parts[1], params)

    # ---- test-control endpoints -----------------------------------------

    def _handle_test(self, path, params):
        if path == "/__test__/inject_update":
            with STATE.lock:
                upd = dict(params)
                upd["update_id"] = STATE.next_update_id
                STATE.next_update_id += 1
                STATE.pending_updates.append(upd)
            return self._send_json({"ok": True, "update_id": upd["update_id"]})

        if path == "/__test__/calls":
            with STATE.lock:
                return self._send_json({"calls": list(STATE.calls)})

        if path == "/__test__/reset":
            with STATE.lock:
                STATE.calls.clear()
                STATE.pending_updates.clear()
                STATE.overrides.clear()
            return self._send_json({"ok": True})

        if path == "/__test__/set_chat_member":
            cid = str(params.get("chat_id"))
            with STATE.lock:
                STATE.chat_members[cid] = {
                    "status": params.get("status", "member"),
                    "can_change_info": params.get("can_change_info") in
                    ("1", "true", "True", True),
                }
            return self._send_json({"ok": True})

        if path == "/__test__/set_override":
            method = params.get("method")
            body_raw = params.get("body")
            override_body = (json.loads(body_raw)
                              if isinstance(body_raw, str) else body_raw) or {}
            code = int(params.get("code") or 200)
            with STATE.lock:
                STATE.overrides[method] = {"body": override_body, "code": code}
            return self._send_json({"ok": True})

        if path == "/__test__/clear_override":
            method = params.get("method")
            with STATE.lock:
                STATE.overrides.pop(method, None)
            return self._send_json({"ok": True})

        return self._send_json({"ok": False, "description": "unknown test endpoint"}, 404)

    # ---- fake Bot API -----------------------------------------------------

    def _handle_bot_method(self, method, params):
        record(method, params)

        with STATE.lock:
            override = STATE.overrides.get(method)
        if override is not None:
            return self._send_json(override["body"], override.get("code", 200))

        if method == "getMe":
            return self._send_json({"ok": True, "result": STATE.bot_user})

        if method == "getUpdates":
            offset = int(params.get("offset") or 0)
            timeout = float(params.get("timeout") or 0)
            deadline = time.time() + timeout
            while True:
                with STATE.lock:
                    ready = sorted(
                        (u for u in STATE.pending_updates
                         if u["update_id"] >= offset),
                        key=lambda u: u["update_id"])
                if ready or time.time() >= deadline:
                    return self._send_json({"ok": True, "result": ready[:100]})
                time.sleep(0.05)

        if method == "getFile":
            fid = params.get("file_id", "")
            # Mirrors the cloud Bot API's real refusal above 20 MB.
            if fid == "too_big":
                return self._send_json({"ok": False, "error_code": 400,
                                        "description": "Bad Request: file is too big"}, 400)
            return self._send_json({"ok": True, "result": {
                "file_id": fid, "file_path": f"files/{fid}.bin"}})

        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            with STATE.lock:
                STATE.next_message_id += 1
                mid = STATE.next_message_id
            return self._send_json({"ok": True, "result": {
                "message_id": mid,
                "chat": {"id": _to_int(params.get("chat_id"))},
                "text": params.get("text", params.get("caption", "")),
                "date": int(time.time()),
            }})

        if method in ("editMessageText", "deleteMessage", "answerCallbackQuery",
                      "setChatTitle", "setMyCommands", "deleteMyCommands",
                      "setChatMenuButton", "sendMessageDraft",
                      "setMessageReaction", "editMessageReplyMarkup"):
            return self._send_json({"ok": True, "result": True})

        if method == "getChat":
            cid = params.get("chat_id")
            return self._send_json({"ok": True, "result": {
                "id": _to_int(cid), "title": f"FakeChat{cid}", "type": "group"}})

        if method == "getChatMember":
            cid = str(params.get("chat_id"))
            with STATE.lock:
                cm = STATE.chat_members.get(
                    cid, {"status": "member", "can_change_info": False})
            return self._send_json({"ok": True, "result": {
                "status": cm["status"],
                "can_change_info": cm["can_change_info"],
                "user": {"id": _to_int(params.get("user_id"))},
            }})

        # Unrecognised method: default to a harmless ok/true rather than 404,
        # so a script exercising an API call this fake hasn't modelled yet
        # degrades quietly instead of crashing the whole test.
        return self._send_json({"ok": True, "result": True})


def _to_int(v):
    try:
        return int(v)
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    print(port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
