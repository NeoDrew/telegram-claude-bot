#!/usr/bin/env python3
"""
test_ask_user_send_429.py - an ask_user question survives a Telegram 429, and
one that can't be delivered is never waited on.

BUG FIXED: a rate-limited sendMessage was swallowed, so the question never
appeared, but the server still wrote .ask_user_pending.json and waited 5
minutes. The listener then took the user's next ordinary message as the
answer to a question they never saw. Now a 429 is retried after its
retry_after, and if the send still fails Claude is told at once, with no
pending file, so the next message is a normal message.

Run: python3 tests/test_ask_user_send_429.py
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")


def load(room):
    os.environ.update(ROOM_DIR=room, CHAT_ID="1", TELEGRAM_BOT_TOKEN="0:fake")
    spec = importlib.util.spec_from_file_location(
        "ask_user_mcp_server", os.path.join(SCRIPTS, "ask_user_mcp_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time.sleep = lambda s: None  # no real waiting in tests
    return mod


RATE_LIMITED = {"ok": False, "error_code": 429,
                "description": "Too Many Requests: retry after 1",
                "parameters": {"retry_after": 1}}


def test_retry_after_429(failures):
    room = tempfile.mkdtemp()
    mod = load(room)
    sends = []

    def fake_post(endpoint, **kw):
        if endpoint == "sendMessage":
            sends.append(kw)
            return RATE_LIMITED if len(sends) == 1 else {"ok": True, "result": {"message_id": 55}}
        return {}

    mod.tg_post = fake_post
    mod.TIMEOUT = 2
    answer_path = os.path.join(room, ".ask_user_answer")
    pending_path = os.path.join(room, ".ask_user_pending.json")

    def answer_when_pending():
        for _ in range(200):
            if os.path.exists(pending_path):
                open(answer_path, "w").write("Yes")
                return
            time.sleep(0.01)
    t = threading.Thread(target=answer_when_pending)
    t.start()
    result = mod.handle_ask_user({"prompt": "Ship it?", "options": ["Yes", "No"]})
    t.join()
    if len(sends) != 2:
        failures.append(f"429 was not retried once: {len(sends)} sends")
    if result != "Yes":
        failures.append(f"answer after a retried send was lost: {result!r}")


def test_undeliverable_is_not_waited_on(failures):
    room = tempfile.mkdtemp()
    mod = load(room)
    mod.SEND_RETRY_BUDGET_S = 0  # any wait exceeds the budget
    mod.tg_post = lambda endpoint, **kw: RATE_LIMITED
    mod.TIMEOUT = 5
    start = time.monotonic()
    result = mod.handle_ask_user({"prompt": "Ship it?", "options": ["Yes", "No"]})
    if "could not be delivered" not in result:
        failures.append(f"Claude was not told the question failed to send: {result!r}")
    if time.monotonic() - start > 1:
        failures.append("the server waited for an answer to an unsent question")
    if os.path.exists(os.path.join(room, ".ask_user_pending.json")):
        failures.append("a pending file was written for an unsent question, so the "
                        "user's next message would be taken as its answer")


def test_network_error_gives_up(failures):
    room = tempfile.mkdtemp()
    mod = load(room)
    calls = []
    mod.tg_post = lambda endpoint, **kw: calls.append(1) or {}
    result = mod.handle_ask_user({"prompt": "Ship it?"})
    if "could not be delivered" not in result:
        failures.append(f"network failure not reported: {result!r}")
    if len(calls) != 3:
        failures.append(f"expected 3 attempts on a network failure, got {len(calls)}")


def main():
    failures = []
    test_retry_after_429(failures)
    test_undeliverable_is_not_waited_on(failures)
    test_network_error_gives_up(failures)
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ok: 429 retried, undeliverable question reported and not waited on\n\nall passed")


if __name__ == "__main__":
    main()
