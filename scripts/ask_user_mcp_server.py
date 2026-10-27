#!/usr/bin/env python3
"""
MCP stdio server bridging Claude's ask_user tool to Telegram interactive messages.

Claude calls ask_user(prompt, options?) → this server sends an inline-keyboard
message to Telegram, then blocks polling for state/rooms/<chat>/.ask_user_answer.
The listener writes the answer there (text reply or button tap via ask_answer.sh).

Environment (set by spawn_claude.sh via --mcp-config env block):
  ROOM_DIR            - absolute path to state/rooms/<chat_id>/
  CHAT_ID             - Telegram chat id for this room
  TELEGRAM_BOT_TOKEN  - bot API token
  TELEGRAM_API_BASE   - Bot API base URL, defaults to the real endpoint.
                        Overridable so tests/e2e/ can point this at
                        fakes/fake_telegram_server.py.
"""
import json, os, sys, time, pathlib, urllib.request, urllib.parse

ROOM_DIR  = pathlib.Path(os.environ["ROOM_DIR"])
CHAT_ID   = os.environ["CHAT_ID"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE  = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
API       = f"{API_BASE}/bot{BOT_TOKEN}"

PENDING = ROOM_DIR / ".ask_user_pending.json"
ANSWER  = ROOM_DIR / ".ask_user_answer"
TIMEOUT = 300

def tg_post(endpoint, **kwargs):
    payload = {}
    for k, v in kwargs.items():
        payload[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    data = urllib.parse.urlencode(payload).encode()
    req  = urllib.request.Request(f"{API}/{endpoint}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return {}

def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_question(prompt, options):
    text = f"<b>Claude needs your input:</b>\n\n{html_escape(prompt)}"
    rows = []
    for i, opt in enumerate(options):
        # Use index-based callback_data to stay within Telegram's 64-byte limit.
        # ask_answer.sh resolves __idx__N back to the option text via the pending JSON.
        rows.append([{"text": opt, "callback_data": f"act:ask_answer __idx__{i}"}])
    rows.append([{"text": "Skip / cancel question", "callback_data": "act:ask_answer __skip__"}])
    if not options:
        text += "\n\n<i>Reply with a message to answer.</i>"
    resp = tg_post("sendMessage",
                   chat_id=CHAT_ID,
                   text=text,
                   parse_mode="HTML",
                   reply_markup={"inline_keyboard": rows})
    return resp.get("result", {}).get("message_id") or 0

def handle_ask_user(arguments):
    prompt  = arguments.get("prompt", "")
    options = arguments.get("options") or []

    msg_id = send_question(prompt, options)
    PENDING.write_text(json.dumps({
        "prompt": prompt,
        "options": options,
        "msg_id": msg_id
    }))

    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if ANSWER.exists():
            try:
                answer = ANSWER.read_text().strip()
            except OSError:
                time.sleep(0.1)
                continue
            try:
                ANSWER.unlink()
            except OSError:
                pass
            try:
                PENDING.unlink()
            except OSError:
                pass
            if answer == "__skip__":
                return "[question skipped by user]"
            return answer or "[no answer provided]"
        time.sleep(0.5)

    try:
        PENDING.unlink()
    except OSError:
        pass
    return "[no response, question timed out after 5 minutes]"

TOOL_DEF = {
    "name": "ask_user",
    "description": (
        "Ask the user a question in their Telegram chat and wait for the answer. "
        "Use it whenever you offer a choice between a few concrete options (yes/no, "
        "A or B, which of these): pass them as options[] and they appear as buttons "
        "the user taps on their phone, instead of a list they have to type back. "
        "Omit options for a free-text answer. Waits up to 5 minutes."
    ),
    # Load this tool up front instead of behind tool search: a deferred tool
    # has to be looked up before every use, so Claude just asked in prose.
    "_meta": {"anthropic/alwaysLoad": True},
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt":  {"type": "string",  "description": "The question to display"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "Optional choices (shown as inline keyboard buttons)"}
        },
        "required": ["prompt"]
    }
}

def reply(rid, result):
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)

def err(rid, code, msg):
    print(json.dumps({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": code, "message": msg}}), flush=True)

def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue

        rid    = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == "initialize":
            reply(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ask-user", "version": "1.0"}
            })
        elif method in ("notifications/initialized", "notifications/cancelled"):
            pass
        elif method == "tools/list":
            reply(rid, {"tools": [TOOL_DEF]})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if name == "ask_user":
                answer = handle_ask_user(args)
                reply(rid, {"content": [{"type": "text", "text": answer}]})
            else:
                err(rid, -32601, f"Unknown tool: {name}")
        else:
            if rid is not None:
                err(rid, -32601, f"Unknown method: {method}")

main()
