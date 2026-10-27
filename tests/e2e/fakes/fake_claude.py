#!/usr/bin/env python3
"""
fake_claude.py - a stand-in for the real `claude` CLI binary, for E2E tests.

spawn_claude.sh execs this in place of the real claude binary when
CLAUDE_BIN_OVERRIDE points at it (see scripts/spawn_claude.sh). It mimics
just enough of claude's stream-json I/O contract to drive the real
listener.sh -> spawn_claude.sh -> stream_to_telegram.py pipeline end to end,
without touching the real Claude API:

  - Persistent mode (--input-format=stream-json present): reads one NDJSON
    user-message line per turn from stdin (mirrors `tail -f inbox.ndjson`),
    and for each line emits the next scripted turn's stream-json events to
    stdout. Keeps reading until stdin closes (the real shutdown path) or a
    scripted turn says to exit (simulating a crash or rate limit).
  - One-shot mode (no --input-format flag, used by CLAUDE_SILENT_SPAWN=1
    scheduled jobs): the prompt is the last argv token; emits turns[0] (or a
    default) once and exits.

What to emit per turn is driven by a JSON fixture file named by the
FAKE_CLAUDE_SCRIPT env var. Turns can be scoped per room (keyed by the
CHAT_ID env var spawn_claude.sh always exports) so one fixture can drive a
multi-room scenario where each room gets its own scripted conversation
without stealing each other's turns:

  {"rooms": {
      "555000111": {"turns": [{"text": "reply for this DM"}]},
      "-4900000001": {"turns": [{"text": "reply for this group"}]}
   },
   "turns": [...]}   # fallback for any room not listed in "rooms"

A flat {"turns": [...]} fixture (no "rooms" key) still works and is treated
as the fallback for every room, which is enough for single-room scenarios.

Turns are consumed in order, one per inbox line (persistent mode). Running
out of scripted turns falls back to a generic default reply rather than
erroring, so tests only need to script the turns they actually care about.
Consumption is tracked per (script path, room) in a small state file next
to the fixture, so it survives across separate fake_claude.py process
launches (matters for rate-limit/crash scenarios, where the resumed spawn
is a brand-new process) without one room's spawn stealing another's turn.

Turn shapes:
  {"text": "..."}                                   - normal streamed reply
  {"echo": true}                                     - reply text IS the
    raw inbox line's message content, verbatim. Lets a test assert on
    exactly what content/prefix reached claude (sender-name prefixing,
    embedded-newline round-tripping, unicode, etc) instead of guessing.
  {"rate_limit": {"resets_in": N, "rate_limit_type": "...",
                  "native_banner": "..."}, "exit_code": N}
                                                       - optional native_banner
    streams as ordinary text FIRST (mirrors Claude Code's own "You've hit
    your session limit · resets ..." banner), then rate_limit_event + an
    is_error 429-shaped result, then exit (mirrors a real 429 kill).
  {"crash": true, "exit_code": N}                     - exit immediately,
    no result envelope at all (mirrors OOM / network drop / internal error).
  {"hang": true}                                      - emit message_start
    plus one partial text delta, then block forever (until SIGTERM/SIGKILL).
    For testing /cancel against a genuinely in-flight turn.

Any text/echo turn also accepts, independent of the shape above:
  "duration_ms": N   - adds a real duration_ms to the result envelope.
    Omitted by default (unlike real claude, which always sends one) since
    most tests don't care; opt in to exercise stream_to_telegram.py's
    "⏱ Worked for Xs" turn-duration footer, which is gated on this field
    being present and otherwise never fires against the fake at all.
  "ctx_pct": N       - synthesizes a modelUsage/usage.iterations shape so
    ctx_pct_from_result() computes exactly N%. Combine with duration_ms to
    exercise the footer's context-percentage display, or to simulate a
    large turn-over-turn drop (auto-compact detection: >25 percentage
    points lower than the previous turn's ctx_pct adds a "✂️ compacted
    X%→Y%" note to the footer).
"""

import json
import os
import signal
import sys
import time


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def _chunks(text, size=40):
    if not text:
        yield ""
        return
    for i in range(0, len(text), size):
        yield text[i:i + size]


def emit_text_turn(text, duration_ms=None, ctx_pct=None):
    emit({"type": "stream_event", "event": {"type": "message_start"}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text"}}})
    for chunk in _chunks(text):
        emit({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": chunk}}})
        time.sleep(0.01)
    emit({"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}})
    emit({"type": "stream_event", "event": {"type": "message_stop"}})
    result = {"type": "result", "subtype": "success", "is_error": False,
              "result": text}
    # A real result envelope always carries duration_ms, and stream_to_
    # telegram.py's "Worked for Xs" turn footer is gated on it being
    # present; the fake never included it, so that whole code path (and
    # its own "large context drop -> compacted" auto-detection) went
    # completely unexercised by every e2e test, including a real bug it
    # once had. Opt-in per turn so tests that don't care about the footer
    # stay exactly as before.
    if duration_ms is not None:
        result["duration_ms"] = duration_ms
    if ctx_pct is not None:
        # ctx_pct_from_result() reads modelUsage[*].contextWindow and the
        # LAST usage.iterations entry's token counts, then divides. Fake a
        # round context window and put all the tokens in input_tokens so
        # the resulting percentage matches ctx_pct exactly.
        ctx_window = 200000
        result["modelUsage"] = {"fake-model": {"contextWindow": ctx_window}}
        result["usage"] = {"iterations": [{
            "input_tokens": round(ctx_pct / 100 * ctx_window),
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }]}
    emit(result)


def emit_rate_limit_turn(info):
    resets_at = int(time.time()) + int(info.get("resets_in", 300))
    # native_banner mirrors the real Claude Code CLI's own rate-limit
    # message ("You've hit your session limit · resets ...") streaming as
    # ordinary text BEFORE the structured rate_limit_event/is_error result
    # closes the turn out. Reproduces the double-notification bug (this
    # banner landing as its own message immediately followed by the
    # bot's own separately-formatted "⏸ Rate limited" message).
    banner = info.get("native_banner")
    if banner:
        emit({"type": "stream_event", "event": {"type": "message_start"}})
        emit({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}}})
        for chunk in _chunks(banner):
            emit({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": chunk}}})
        emit({"type": "stream_event", "event": {
            "type": "content_block_stop", "index": 0}})
        emit({"type": "stream_event", "event": {"type": "message_stop"}})
    emit({"type": "rate_limit_event", "rate_limit_info": {
        "status": info.get("status", "rejected"),
        "resetsAt": resets_at,
        "rateLimitType": info.get("rate_limit_type", "five_hour"),
        "utilization": info.get("utilization", 1.0),
    }})
    emit({"type": "result", "subtype": "error_rate_limit", "is_error": True,
          "result": banner or ("Claude AI usage limit reached|" + str(resets_at)),
          "api_error_status": "429"})


def emit_auth_failure_turn(info):
    emit({"type": "result", "subtype": "error_auth", "is_error": True,
          "result": info.get("text", "Invalid API key. Please run /login."),
          "api_error_status": info.get("api_error_status", "401")})


def emit_hang_turn():
    # A real hang happens mid TOOL EXECUTION, not mid text generation: once
    # claude starts streaming prose it keeps emitting deltas continuously,
    # so a stalled *text* block never renders past the initial buffered
    # chunk (stream_to_telegram.py only flushes when a NEW event arrives,
    # there's no independent timer). A tool call is the realistic "stuck"
    # signal: its content_block_stop sends its own standalone, unbuffered
    # Telegram message the moment the call starts, then claude legitimately
    # goes quiet until the tool returns, exactly what real long-running
    # Bash/browser-automation calls look like.
    emit({"type": "stream_event", "event": {"type": "message_start"}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "tool_hang",
                           "name": "Bash"}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta",
                  "partial_json": '{"command":"sleep 999"}'}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}})
    # No further events: the tool call never returns. Ends only on an
    # interrupt control request (the 🛑 cancel button, which the real CLI
    # answers by ending the turn with error_during_execution) or a signal
    # (a force stop SIGTERMs the whole spawn's process group). Messages
    # that arrive meanwhile are kept for after the turn, like the real CLI.
    while True:
        line = sys.stdin.readline()
        if not line:
            signal.pause()
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if (isinstance(obj, dict) and obj.get("type") == "control_request"
                and (obj.get("request") or {}).get("subtype") == "interrupt"):
            emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": obj.get("request_id")}})
            emit({"type": "result", "subtype": "error_during_execution",
                  "is_error": True, "duration_ms": 1000})
            return
        _stashed.append(line)


_stashed = []   # inbox lines read during a hang, answered after it


def _stdin_lines():
    while True:
        if _stashed:
            yield _stashed.pop(0)
            continue
        line = sys.stdin.readline()
        if not line:
            return
        yield line


def load_script():
    path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        sys.stderr.write(f"fake_claude.py: failed to load {path}: {e}\n")
        return {}


def turns_for_this_room(script):
    chat_id = os.environ.get("CHAT_ID", "")
    rooms = script.get("rooms") or {}
    if chat_id in rooms:
        return rooms[chat_id].get("turns", [])
    return script.get("turns", [])


def _turn_state_path():
    # A rate-limit/crash turn ends THIS process; spawn_claude.sh's auto-
    # resume then execs a brand-new fake_claude.py process for the next
    # turn, and a nudge to a DIFFERENT room spawns yet another independent
    # process. Both must see a durable, PER-ROOM cursor into the turn list,
    # not an in-memory index or a global-per-script one (a global one would
    # let an unrelated room's spawn steal this room's next turn).
    script_path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not script_path:
        return None
    chat_id = os.environ.get("CHAT_ID", "noroom")
    return f"{script_path}.state.{chat_id}"


def _load_turn_index():
    path = _turn_state_path()
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _save_turn_index(idx):
    path = _turn_state_path()
    if path:
        with open(path, "w") as f:
            f.write(str(idx))


def run_turn(turn, turn_index, raw_line):
    if turn.get("delay"):
        # Lets a test reliably observe transient on-disk state (e.g. a
        # marker file touched then cleared once the turn completes)
        # instead of racing a turn that would otherwise finish before the
        # test's own check gets a chance to run.
        time.sleep(float(turn["delay"]))
    if turn.get("hang"):
        emit_hang_turn()
        return
    if "rate_limit" in turn:
        emit_rate_limit_turn(turn["rate_limit"])
        sys.exit(turn.get("exit_code", 1))
    if "auth_failure" in turn:
        emit_auth_failure_turn(turn["auth_failure"])
        exit_code = turn.get("exit_code")
        if exit_code is not None:
            sys.exit(exit_code)
        return
    if turn.get("crash"):
        sys.exit(turn.get("exit_code", 1))
    duration_ms = turn.get("duration_ms")
    ctx_pct = turn.get("ctx_pct")
    if turn.get("echo"):
        text = _extract_inbox_text(raw_line)
        emit_text_turn(text, duration_ms=duration_ms, ctx_pct=ctx_pct)
    else:
        emit_text_turn(turn.get("text", f"(fake claude default reply #{turn_index})"),
                        duration_ms=duration_ms, ctx_pct=ctx_pct)
    exit_code = turn.get("exit_code")
    if exit_code is not None:
        sys.exit(exit_code)


def _extract_inbox_text(raw_line):
    """Pull the plain message content back out of one NDJSON inbox line,
    for {"echo": true} turns. Falls back to the raw line if the shape is
    unexpected (never crashes the fake on malformed input)."""
    try:
        obj = json.loads(raw_line)
        content = obj.get("message", {}).get("content", "")
        return content if isinstance(content, str) else str(content)
    except Exception:
        return raw_line


def main():
    args = sys.argv[1:]
    persistent = any(a.startswith("--input-format=stream-json") for a in args)
    script = load_script()
    turns = turns_for_this_room(script)

    if not persistent:
        idx = _load_turn_index()
        turn = turns[idx] if idx < len(turns) else {}
        _save_turn_index(idx + 1)
        prompt = args[-1] if args else ""
        run_turn(turn, idx, json.dumps(
            {"message": {"content": prompt}}))
        sys.exit(0)

    replay = "--replay-user-messages" in args
    idx = _load_turn_index()
    for raw_line in _stdin_lines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)  # content parsed lazily below
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "user":
            continue   # control requests (interrupts) are not turns
        if replay:
            # Like the real CLI: echo each line the moment it is read.
            print(json.dumps({"type": "user", "message": obj.get("message"),
                              "isReplay": True}), flush=True)
        turn = turns[idx] if idx < len(turns) else {}
        _save_turn_index(idx + 1)
        run_turn(turn, idx, line)
        idx += 1
    # stdin closed cleanly (inbox tail broke / spawn torn down normally).
    sys.exit(0)


if __name__ == "__main__":
    main()
