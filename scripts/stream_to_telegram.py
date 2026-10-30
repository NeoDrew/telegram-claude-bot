#!/usr/bin/env python3
"""
stream_to_telegram.py - parse claude's stream-json output and push it to
Telegram by editing a single message in place as text streams in.

Reads NDJSON lines from stdin. Talks to Telegram Bot API directly (does not
shell out to tg.sh on the hot path, to avoid 100s of subprocess spawns).

Strategy:
  - On the first model event, send "…" placeholder and save the message_id.
  - For each text_delta, append to a buffer. Edit the live message
    EDIT_INTERVAL seconds after the previous edit (throttle).
  - Tool-use and thinking blocks DO NOT produce separate messages. The live
    edit-in-place message stays as the last message in the chat, so the
    user always sees the latest text at the bottom without a wall of
    tool-call pings sitting underneath.
  - When the buffer would overflow MAX_MSG_CHARS, finalize the current live
    message and start a new one carrying the latest chunk.
  - On message_stop or EOF, force-flush.

Failure handling:
  - Telegram 429 → respect retry_after, retry once.
  - Telegram 400 "not modified" → silently skip.
  - JSON parse errors per line → log and continue.
  - Network failures → log and drop that edit (keep streaming).
  - Mid-stream crash → append " (stream broken)" to the last edit.
  - No output at all → send "(claude returned no output)".
"""

import sys
import os
import json
import re
import time
import random
import signal
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from md_telegram import md_to_telegram_html  # noqa: E402
import metrics  # noqa: E402
import bot_config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
LOG_FILE = REPO / "logs" / "stream.log"
# rate_limit_event snapshots cached here. /usage reads as fallback to scrape.
USAGE_CACHE = REPO / "state" / "usage_cache.json"

def _log_sent(mid):
    """Append (ts, message_id) to this room's sent_msgs.log so /clear can
    deleteMessage them later. Best-effort; never raises.

    Refuses chat_id "0", that's not a valid Telegram chat and only ever
    comes from the smoke test or a malformed update. Writing to
    state/rooms/0/ littered the /forget listing with a ghost room.
    """
    if not mid or not TELEGRAM_CHAT_ID or str(TELEGRAM_CHAT_ID) == "0":
        return
    try:
        room_dir = REPO / "state" / "rooms" / str(TELEGRAM_CHAT_ID)
        room_dir.mkdir(parents=True, exist_ok=True)
        with open(room_dir / "sent_msgs.log", "a") as f:
            f.write(f"{int(time.time())}\t{mid}\n")
    except Exception:
        pass

EDIT_INTERVAL = 0.8      # seconds between editMessageText calls per message
# Groups get a slower pace. Telegram lets a bot send about 20 messages a
# minute to one group, and edits count, so 0.8s ran into a 429 within the
# first half minute of a long reply; every 429 then froze the bubble for
# 10 to 20s before it jumped ahead (stream.log: about 100 a day). One edit
# every 3s stays near that budget, so the text grows steadily instead.
# DMs stream with drafts and are unaffected. Override: BOT_GROUP_EDIT_INTERVAL.
GROUP_EDIT_INTERVAL = 3.0
MAX_HTML_CHARS = 4000    # Telegram counts the HTML-rendered length against
                         # its 4096 ceiling. Markdown like **bold** / `code` /
                         # pipe-tables expands significantly (a fenced table
                         # can nearly double); the proactive rotate measures
                         # this length, not the markdown-buffer length, so
                         # heavily-formatted messages don't sail past 4096
                         # and then 400 forever.
# Draft frames (sendMessageDraft) are lighter than a full editMessageText and
# the API is purpose-built for streaming, so we push them more often for a
# snappier fill-in. Still throttled so we don't hammer the API. Override with
# BOT_DRAFT_INTERVAL (seconds).
DRAFT_INTERVAL = 0.35
MAX_MSG_CHARS = 3800     # safety margin under Telegram's 4096 limit
HTTP_TIMEOUT = 20        # seconds per Telegram API call

# Previously, tool markers could land well after the tool's own output: a
# photo sent by a Bash call could land as message N while the tool marker
# for that same call only landed as message N+1, several seconds later,
# with no 429s or errors logged in between.
#
# Root cause: the main loop below (`for line in sys.stdin`) reads and
# reacts to claude's output ONE LINE AT A TIME, and every real Telegram
# API call it makes (an editMessageText, a sendMessageDraft, a tool-marker
# sendMessage) is a synchronous, blocking network round-trip, up to
# HTTP_TIMEOUT (20s) if a single call is slow. During a long streamed
# response, that loop makes MANY such calls (every DRAFT_INTERVAL/
# EDIT_INTERVAL while text is flowing), each one a real few-hundred-ms-plus
# round trip. Claude keeps writing to the pipe at its own pace regardless
# of whether this loop is caught up; if the cumulative time spent blocked
# in those calls exceeds the time claude takes to write more output, the
# loop falls behind, and a backlog builds up in the stdin pipe buffer, an
# event that claude emitted a moment ago (like the content_block_stop that
# should trigger a tool marker) might not actually get READ by this loop,
# let alone acted on, until several seconds later, simply because the loop
# is still working through everything queued ahead of it.
#
# A tool's own side effect (e.g. a Bash call to notify-cmd/tg.sh sending a
# photo) is NOT subject to any of this: Claude Code's SDK runs that tool
# as its own subprocess, completely independent of this streamer and this
# backlog, so it can complete and land in the chat in about a second while
# the marker announcing it is still queued behind unrelated earlier work.
#
# Fix: background sender threads own the actual outbound network calls for
# the two genuinely hot-path spots (maybe_flush's repeated draft/edit
# updates, and the mid-turn, blocking=False, tool-marker and diff-message
# sends). The main loop hands off a ready-to-send snapshot and moves
# straight on to the next stdin line instead of waiting for the HTTP
# round-trip. True end-of-turn delivery (commit_live(blocking=True):
# message_stop, the result envelope, end-of-stdin, salvage, the turn-end
# footer) stays fully synchronous and unchanged, that path already has
# its own careful retry/wait logic for the one case where losing the
# message matters most, and it isn't the hot path this bug lives in.
#
# TWO separate queues, each with its own single worker, not one shared
# queue: tried one queue first and test_streamer_no_backlog_block.py
# caught the flaw, a slow tool-marker send would sit ahead of a LATER,
# unrelated maybe_flush job in the same FIFO line, so commit_live()
# waiting on "my own box's edit jobs are done" (see _wait_for_flush_jobs
# below) ended up waiting on the slow marker anyway, just one hop removed.
# Splitting them means a slow tool-marker send only ever delays OTHER tool
# markers (still correctly ordered among themselves), never the growing
# edit-in-place box, and vice versa. Each queue keeps its own single
# worker so sends WITHIN each channel still land in decided order.
import threading
import queue as _queue_mod

_edit_queue: "_queue_mod.Queue" = _queue_mod.Queue()      # maybe_flush's draft/edit sends
_marker_queue: "_queue_mod.Queue" = _queue_mod.Queue()    # tool-marker / diff-message sends
# Protects tg_api's shared rate-limit bookkeeping (_rate_limited_until,
# _rl_backoff_level, etc.), now touched from multiple threads. Scoped
# narrowly around just that bookkeeping (see tg_api itself), NOT held
# across the actual network call, or one slow call would serialize every
# other thread behind it, defeating the point of splitting the queues at
# all. MUST be reentrant: _cancel_handler (SIGTERM/SIGINT, e.g. /cancel)
# runs ON the main thread and can itself call tg_api/tg_edit/tg_clear_draft.
# Python delivers a signal by interrupting the main thread's current C call
# and running the handler right there, if that happens while the main
# thread is already inside this lock, a plain Lock would self-deadlock the
# moment the handler tried to send its own cancellation message. An RLock
# lets the same thread re-enter safely.
_tg_api_lock = threading.RLock()


def _make_sender_worker(q):
    def _worker():
        while True:
            job = q.get()
            try:
                job()
            except Exception as e:
                log(f"background sender job raised: {e}")
            finally:
                q.task_done()
    return _worker


threading.Thread(target=_make_sender_worker(_edit_queue), daemon=True).start()
threading.Thread(target=_make_sender_worker(_marker_queue), daemon=True).start()


def _bg_edit(fn):
    """Enqueue a zero-arg callable on the edit-box sender (maybe_flush's
    draft/edit updates). Single worker + FIFO = strict ordering within
    this channel."""
    _edit_queue.put(fn)


def _bg_marker(fn):
    """Enqueue a zero-arg callable on the tool-marker/diff-message sender.
    Separate from _bg_edit deliberately, see the module-level docstring
    above for why a slow send here must never delay the edit-in-place box,
    or vice versa. Single worker + FIFO = markers still land in the order
    tools were actually called.

    The job's send-order ticket is taken HERE, on the enqueuing thread, in
    program order, not when the worker gets round to it. See _seq_take."""
    ticket = _seq_take()

    def _ticketed():
        _seq_wait(ticket)
        _seq_tls.held = True
        try:
            fn()
        finally:
            _seq_tls.held = False
            _seq_done(ticket)
    _marker_queue.put(_ticketed)


# New-message send order. Telegram positions a message by when its
# sendMessage lands, so two creates racing on different paths (a tool
# marker on _marker_queue vs the next bubble's placeholder sent straight
# from the main loop) can land in the wrong order: the next reply above the
# tool marker that should precede it.
# Every sendMessage takes a ticket in issue order and may only go out once
# every earlier ticket has finished. Edits to an existing message take no
# ticket (they never move a message), so streaming into a live bubble never
# waits on this. Bounded by SEQ_WAIT_MAX so one hung send can't wedge the
# rest forever.
SEQ_WAIT_MAX = HTTP_TIMEOUT + 5
_seq_cv = threading.Condition()
_seq_next = [0]      # next ticket to hand out
_seq_serving = [0]   # every ticket below this has finished
_seq_finished = set()
_seq_bypass = [False]  # set by the cancel handler: order no longer matters
_seq_tls = threading.local()


def _seq_take():
    with _seq_cv:
        t = _seq_next[0]
        _seq_next[0] += 1
        return t


def _seq_wait(ticket):
    deadline = time.monotonic() + SEQ_WAIT_MAX
    with _seq_cv:
        while _seq_serving[0] < ticket and not _seq_bypass[0]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log(f"send-order wait for ticket {ticket} timed out; sending anyway")
                return
            _seq_cv.wait(remaining)


def _seq_done(ticket):
    with _seq_cv:
        _seq_finished.add(ticket)
        while _seq_serving[0] in _seq_finished:
            _seq_finished.discard(_seq_serving[0])
            _seq_serving[0] += 1
        _seq_cv.notify_all()


# maybe_flush()'s draft sends are a special case: tg_send_draft() reads the
# CURRENT `_draft_id` global at execution time (not at enqueue time, see
# tg_send_draft's own docstring), and commit_live() needs `_draft_used_plain`
# (which format the most recently CONFIRMED draft frame actually used) to
# finalise byte-identically and avoid a flash. Both are only safe once any
# maybe_flush job for the box currently being finalised has actually run.
#
# Draining the WHOLE _send_queue in commit_live() to guarantee this was
# tried first and rejected: proven by this file's own test suite
# (test_streamer_no_backlog_block.py) to reintroduce a scaled-down version
# of the exact backlog bug this change fixes, a commit_live(blocking=False)
# call for a LATER, unrelated tool marker would end up waiting behind an
# EARLIER, still-in-flight maybe_flush job that has nothing to do with it.
# This narrower counter tracks ONLY outstanding maybe_flush jobs (the tool-
# marker and diff-message jobs never touch _draft_id/_draft_used_plain, so
# they're deliberately excluded), commit_live() waits on just this, which
# in the overwhelmingly common case is 0 (nothing in flight) and in the rare
# force=True-overlap case is bounded to however many maybe_flush jobs are
# genuinely still pending, never an unrelated backlog.
_flush_cv = threading.Condition()
_pending_flush_jobs = [0]


def _flush_job_started():
    with _flush_cv:
        _pending_flush_jobs[0] += 1


def _flush_job_finished():
    with _flush_cv:
        _pending_flush_jobs[0] -= 1
        _flush_cv.notify_all()


def _wait_for_flush_jobs():
    with _flush_cv:
        while _pending_flush_jobs[0] > 0:
            _flush_cv.wait()


# "💭 Thinking…" placeholders whose deleteMessage didn't go through. Removing
# the placeholder is a single call, and during a 429 cooldown tg_api skips it
# without trying, so the bubble used to stay forever, right above the turn's
# "Worked for" footer. Anything recorded here is retried at end of turn,
# after the cooldown has cleared and before the footer goes out.
_stale_placeholders: list = []


def _delete_placeholder(message_id):
    resp = tg_api("deleteMessage", {
        "chat_id": TELEGRAM_CHAT_ID, "message_id": message_id,
    })
    if not (resp and resp.get("ok")):
        _stale_placeholders.append(message_id)


def _retry_stale_placeholders():
    """End of turn only: wait out any cooldown, then retry each failed delete
    once. A delete that still fails after the cooldown is a real error (the
    message is already gone, say), so it is dropped rather than retried
    forever."""
    if not _stale_placeholders:
        return
    _wait_for_cooldown("stale placeholder cleanup")
    pending = list(_stale_placeholders)
    _stale_placeholders.clear()
    for mid in pending:
        resp = tg_api("deleteMessage", {
            "chat_id": TELEGRAM_CHAT_ID, "message_id": mid,
        })
        if not (resp and resp.get("ok")):
            log(f"stale thinking placeholder {mid} could not be deleted")


def load_env_var(key):
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


TELEGRAM_BOT_TOKEN = load_env_var("TELEGRAM_BOT_TOKEN")
# Multi-chat: when listener.sh dispatches a message it exports CHAT_ID so
# the streamer sends the reply back to the originating room (DM, group, or
# topic). Falls back to TELEGRAM_CHAT_ID from .env for backwards compat.
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID") or load_env_var("TELEGRAM_CHAT_ID")
# Overridable so the E2E test harness (tests/e2e/) can point this at a local
# fake Bot API server instead of the real Telegram backend. Defaults to the
# real endpoint, so production behaviour is unchanged when unset.
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
API_URL = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}"


def log(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except Exception:
        pass


# When Claude's context is very full it sometimes reverts to generating
# <invoke> XML in plain text instead of native tool_use blocks. Strip those
# before display so raw tool-call XML never reaches Telegram.
_INVOKE_RE = re.compile(
    r'<invoke\b[^>]*>.*?</invoke>',
    re.DOTALL,
)


def _strip_leaked_xml(text: str) -> str:
    return _INVOKE_RE.sub('', text)


def html_format(text):
    """Render markdown subset as Telegram HTML. See scripts/md_telegram.py."""
    return md_to_telegram_html(_strip_leaked_xml(text))


def fmt_duration(ms) -> str:
    """Human "Worked for X" duration from milliseconds. <60s → "Ns"; under an
    hour → "Nm Ns"; otherwise "Nh Nm" (seconds are noise at that scale)."""
    try:
        s = int(round(float(ms) / 1000.0))
    except (TypeError, ValueError):
        return ""
    if s < 1:
        return "<1s"
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def fmt_countdown(seconds: float) -> str:
    """Human "in X" countdown to a rate-limit reset, e.g. "2h15m", "45m",
    "1d3h". A bare clock time ("resets at 5:00 PM") makes the reader do
    their own subtraction against whatever time it is right now; this is
    what people actually asked for instead. Clamped at 0 so a message
    rendered a beat after the reset already passed doesn't show a negative
    duration. Coarse (no seconds), day-scale for the longer limit types
    (weekly, 7-day), not just the common 5-hour case."""
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d{h}h" if h > 0 else f"{d}d"
    if h > 0:
        return f"{h}h{m}m" if m > 0 else f"{h}h"
    return f"{m}m"


def ctx_pct_from_result(ev: dict):
    """Return the current context fill % from a result event, or None.

    Uses the last entry in usage.iterations (the most recent API call in
    the turn) divided by the model's contextWindow from modelUsage. This
    gives the actual context size at turn end, not a cumulative lifetime
    total. Returns a float 0-100 or None when data is missing."""
    model_usage = ev.get("modelUsage") or {}
    usage = ev.get("usage") or {}
    iterations = usage.get("iterations") or []
    if not iterations or not model_usage:
        return None
    ctx_window = None
    for md in model_usage.values():
        w = md.get("contextWindow")
        if w:
            ctx_window = w
            break
    if not ctx_window:
        return None
    last = iterations[-1]
    total = (last.get("input_tokens", 0)
             + last.get("cache_read_input_tokens", 0)
             + last.get("cache_creation_input_tokens", 0))
    return round(total / ctx_window * 100, 1)


_RATE_LIMIT_KEYWORDS = ("429", "rate limit", "rate_limit", "usage limit",
                        "overloaded", "usage_limit_reached", "session limit")

_LIMIT_LABELS: dict[str, str] = {
    "five_hour": "5-hour session limit",
    "hourly": "hourly limit",
    "daily": "daily limit",
    "weekly": "weekly limit",
    "seven_day": "7-day limit",
}
_LIMIT_SHORT: dict[str, str] = {
    "five_hour": "5h",
    "hourly": "1h",
    "daily": "24h",
    "weekly": "7d",
    "seven_day": "7d",
}


def _rate_limit_footer() -> str:
    """Return a footer like ' · ⏳ 5h 73% ⚠️' from the latest rate_limit_event
    snapshot. Anthropic only fills utilization in warning/blocked states."""
    try:
        cache = json.loads(USAGE_CACHE.read_text()) if USAGE_CACHE.exists() else {}
    except Exception:
        return ""
    now_ts = time.time()
    parts = []
    for rtype in ("five_hour", "hourly", "daily", "weekly", "seven_day"):
        info = cache.get(rtype)
        if not info:
            continue
        util = info.get("utilization")
        if util is None:
            continue
        resets = int(info.get("resetsAt") or 0)
        if resets and resets <= now_ts:
            continue
        pct = round(float(util) * 100)
        short = _LIMIT_SHORT.get(rtype, rtype)
        warn = " ⚠️" if pct >= 80 else ""
        parts.append(f"{short} {pct}%{warn}")
    if not parts:
        return ""
    return " · ⏳ " + " · ".join(parts)


def _fmt_error_result(raw_err: str) -> str:
    """Return a Telegram-ready error string for a result is_error event.
    If the error looks like a rate limit, include limit type and reset time
    from the cached rate_limit_event snapshot."""
    low = raw_err.lower()
    is_rate = any(kw in low for kw in _RATE_LIMIT_KEYWORDS)
    if not is_rate:
        return (f"⚠️ Claude hit an error: {raw_err}\n"
                "Your last message wasn't answered; send it again to retry.")

    try:
        cache = json.loads(USAGE_CACHE.read_text()) if USAGE_CACHE.exists() else {}
    except Exception:
        cache = {}

    import datetime as _dt
    now_ts = time.time()

    BLOCKED = ("blocked", "rejected", "rate_limited")
    def _fresh(info):
        resets = int(info.get("resetsAt") or 0)
        return resets == 0 or resets > now_ts

    active = {k: v for k, v in cache.items()
              if (v.get("status") or "") in BLOCKED and _fresh(v)}
    if not active:
        active = {k: v for k, v in cache.items()
                  if float(v.get("utilization") or 0) >= 0.99 and _fresh(v)}
    if not active:
        active = {k: v for k, v in cache.items() if _fresh(v)}

    if not active:
        return "⏸ Rate limited. Messages are queued and will retry automatically."

    lines = []
    for rtype, info in sorted(active.items()):
        label = _LIMIT_LABELS.get(rtype, rtype.replace("_", " ") + " limit")
        resets = int(info.get("resetsAt") or 0)
        if resets and resets > now_ts:
            reset_dt = _dt.datetime.fromtimestamp(resets)
            now_dt = _dt.datetime.now()
            remaining_s = (reset_dt - now_dt).total_seconds()
            if remaining_s < 86400:
                time_str = reset_dt.strftime("%-I:%M %p")
            else:
                time_str = reset_dt.strftime("%a %-I:%M %p")
            countdown = fmt_countdown(remaining_s)
            lines.append(f"*{label}* resets in {countdown} ({time_str})")
        else:
            lines.append(f"*{label}*")

    detail = "\n".join(f"  • {l}" for l in lines)
    return (f"⏸ *Rate limited*\n{detail}\n\n"
            f"_Messages queued and will retry automatically._")


# At most one "⏸ Rate limited" message per room per hour. If a park ever
# falls back to the 5-minute retry (no usable reset time), every retry hits
# the same limit again and would post the same message again, every 5
# minutes. The root cause (the park bound) is fixed separately; this keeps
# any future fallback loop quiet.
RATE_NOTICE_QUIET_S = 3600


def _rate_notice_path():
    return REPO / "state" / "rooms" / str(TELEGRAM_CHAT_ID) / ".rate_notice_at"


def _rate_notice_recent() -> bool:
    try:
        return time.time() - float(_rate_notice_path().read_text().strip()) < RATE_NOTICE_QUIET_S
    except Exception:
        return False


def _mark_rate_notice() -> None:
    try:
        _rate_notice_path().write_text(str(int(time.time())))
    except Exception:
        pass


def _park_room_for_rate_limit() -> None:
    """Write .rate_limited_until to the room state dir so the listener queues
    new messages instead of injecting them into the inbox.

    Called by the is_error handler immediately after surfacing the message.
    The caller then calls sys.exit(0), which causes SIGPIPE to propagate back
    through tee into claude, claude exits non-zero, spawn_claude.sh's exit
    handler fires and runs the full salvage + alert flow. That handler also
    writes .rate_limited_until (harmless overwrite with the same epoch, AS
    LONG AS this write uses the real computed reset time below rather than
    the bare fallback).

    Without this early-exit the persistent claude process stays alive after
    a 429 result and the next inbox message hits the same limit again.

    Previously (found via tests/e2e/test_multi_room_restart_survival.py
    and test_rate_limit_recovery.py flaking under load) this function used
    to compute best_resets from the usage cache and then
    throw it away, always writing now_ts + 300 (5 minutes) on the final
    line regardless of what best_resets said. Since this write lands
    BEFORE spawn_claude.sh's own more accurate post-exit calculation, and
    the idempotency guard in spawn_claude.sh treats an existing
    .rate_limited_until as authoritative when it's not clearly stale, a
    real multi-hour rate limit got written as a 5-minute park. The room
    then retried in 5 minutes, hit the SAME real limit again, wrote
    ANOTHER 5-minute park, and repeated indefinitely, posting a
    rate-limit message every 5 minutes.
    """
    try:
        cache = json.loads(USAGE_CACHE.read_text()) if USAGE_CACHE.exists() else {}
    except Exception:
        cache = {}
    now_ts = int(time.time())
    BLOCKED = ("blocked", "rejected", "rate_limited")
    best_resets = 0
    for info in cache.values():
        if (info.get("status") or "") in BLOCKED:
            r = int(info.get("resetsAt") or 0)
            # Bound covers the weekly window. It was 6h, which threw away a
            # weekly reset days away and parked for 5 minutes instead,
            # producing a "⏸ Rate limited" message every 5 minutes per room.
            if now_ts < r <= now_ts + 8 * 86400:
                best_resets = max(best_resets, r)
    if not best_resets:
        best_resets = now_ts + 300
    room_dir = REPO / "state" / "rooms" / str(TELEGRAM_CHAT_ID)
    try:
        (room_dir / ".rate_limited_until").write_text(str(best_resets))
    except Exception:
        pass


def _safe_split(buf: str, max_html: int = MAX_HTML_CHARS) -> tuple[str, str]:
    """Find a clean split of `buf` such that the head's html-rendered length
    fits under `max_html`. Returns (head, tail) with head + tail == buf
    (modulo fenced-code repair, see below).

    Two-step boundary search:

    1. **Binary search** for the largest character index `safe_idx` whose
       prefix html-renders under the cap. Pure size constraint, no structure
       awareness.

    2. **Paragraph walk-back**: prefer to split at a blank-line boundary
       within the safe region. Markdown constructs in our subset that span
       multiple lines but NOT multiple paragraphs (bold/italic/strike/inline
       code/headings/math, all regex-bounded by \\n) are then guaranteed
       intact on each side. Falls through to the raw char cut only when
       there's no \\n\\n in the safe region (a single pathological paragraph
       > max_html chars).

    3. **Fenced-code repair**: fenced ``` blocks DO span paragraphs and the
       paragraph walk-back can split them. If head ends up with an odd
       number of ``` markers (an unclosed fence), we append \\n``` to head
       and prepend ```\\n to tail so both render as proper monospace
       blocks instead of literal-backtick prose.

    The contract `head + tail == buf` holds only when no fence repair was
    needed; with repair the marker pair is added but the user-visible text
    is preserved across both halves. Pipe tables aren't perfectly handled
    (we'd need to re-emit the header + separator into the tail) but the
    paragraph walk-back keeps them intact in the common case where the
    table is one paragraph in a larger buffer.
    """
    if len(html_format(buf)) <= max_html:
        return buf, ""
    # ── 1. Binary search for the raw safe character index. ────────────────
    lo, hi = 0, len(buf)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if len(html_format(buf[:mid])) <= max_html:
            lo = mid
        else:
            hi = mid
    safe_idx = lo
    if safe_idx == 0:
        # Pathological: not even a one-char prefix fits. Forward progress
        # via midpoint cut so we don't infinite-loop on the caller side.
        safe_idx = len(buf) // 2

    # ── 2. Paragraph walk-back. ──────────────────────────────────────────
    # Find the LATEST "\n\n" whose end position is <= safe_idx; rfind with
    # an end argument restricts the search window to [0, safe_idx). The
    # blank line itself stays with the head, so the tail starts on the
    # next paragraph rather than mid-prefix.
    para = buf.rfind("\n\n", 0, safe_idx)
    if para >= 0:
        cut = para + 2     # split AFTER the blank line
    else:
        cut = safe_idx
    head, tail = buf[:cut], buf[cut:]

    # ── 3. Fenced-code repair. ───────────────────────────────────────────
    # If head has an unclosed ``` block, both halves render as literal
    # backticks + prose. Close head with ```; open tail with ```. Counts
    # are an approximation (a string literal containing ``` would also
    # match) but for claude's typical output `\`\`\`` only ever appears as
    # a fence marker, so the heuristic holds in practice.
    if head.count("```") % 2 == 1:
        head = head + ("\n```" if not head.endswith("\n") else "```")
        tail = ("```\n" if not tail.startswith("\n") else "```") + tail
    return head, tail


_rate_limited_until: float = 0.0
_rate_limited_dropped: int = 0

# Hard cap on how long we'll sleep waiting for a 429 cooldown to expire
# during the final flush. Telegram occasionally hands out 30-60s retry_after
# windows under group-chat load; capping at 90s means a pathological cluster
# still finishes in under two minutes instead of wedging the spawn forever.
COOLDOWN_WAIT_MAX = 90.0


def _wait_for_cooldown(reason: str = "final flush"):
    """Block until the current 429 cooldown expires (capped). Called from
    commit_live so the final delivery isn't silently dropped during a
    rate-limit window, the streamer is at end-of-stream by then, so a
    short sleep doesn't matter, and the alternative is the user seeing
    "…" forever instead of the actual reply.
    """
    now = time.time()
    if now >= _rate_limited_until:
        return
    wait = min(COOLDOWN_WAIT_MAX, _rate_limited_until - now + 0.5)
    log(f"tg waiting {wait:.1f}s for 429 cooldown ({reason})")
    time.sleep(wait)

# Set by tg_api when Telegram returns 400 MESSAGE_TOO_LONG. Consumed (read +
# cleared) by streamer callers via _take_too_long() to drive the split-and-
# rotate recovery path. The previous behaviour was "tg_api returns None and
# logs the error", the streamer treated that the same as any other failure
# and kept retrying the same too-long buffer until the turn ended, which
# silently truncated the final message at whatever the last successful
# edit showed, so replies looked "cut off" in group chats (groups don't get draft-mode, so the edit path is all there is).
_last_too_long: bool = False

# 429 storm tracking. A "storm" is consecutive 429s spanning > 5 minutes.
# When that threshold is crossed we fire one notify-cmd --alert so the user
# knows without checking logs. Resets whenever a call succeeds.
_RL_STORM_THRESHOLD = 300.0        # seconds before alerting
_rl_storm_started: float = 0.0    # epoch when current storm began (0 = no storm)
_rl_storm_alerted: bool = False    # one alert per storm

# Adaptive edit-rate backoff. Previously a single busy room's long-running
# turn could trip Telegram's OWN per-message edit flood limit repeatedly
# (repeated 429s within a single chat, each dropping a batch of frames).
# The streamer waited
# out each retry_after cooldown correctly, but then resumed editing at the
# exact same EDIT_INTERVAL/DRAFT_INTERVAL, so a long turn just re-tripped
# the same ceiling a few minutes later, over and over. Each 429 now bumps
# _rl_backoff_level, which widens maybe_flush's effective interval; a
# sustained streak of clean calls (_RL_DECAY_STREAK in a row, no 429 in
# between) steps it back down by one, so a room that genuinely settles
# down returns to full responsiveness instead of staying throttled
# forever off one old storm.
_RL_BACKOFF_MAX = 4          # interval multiplier caps at 1 + 4*0.75 = 4x
_RL_DECAY_STREAK = 20        # consecutive clean calls needed to step down
_rl_backoff_level: int = 0
_rl_clean_streak: int = 0


def _rl_backoff_multiplier() -> float:
    return 1.0 + _rl_backoff_level * 0.75

# Count 400 "can't parse entities" errors per turn. Logged as a summary at
# turn end so the per-call noise stays out of stream.log.
_parse_entity_errs: int = 0


def _fire_429_storm_alert(duration_s: float) -> None:
    """Push one notify-cmd --alert when Telegram 429 rate-limiting has been
    sustained beyond _RL_STORM_THRESHOLD. Called at most once per storm."""
    import os, subprocess
    mins = int(duration_s // 60)
    label = ""
    try:
        lf = REPO / "state" / "rooms" / str(TELEGRAM_CHAT_ID) / "label"
        label = lf.read_text().strip()
    except Exception:
        pass
    tag = f"[{label}]" if label else "[tg]"
    body = (f"Telegram rate-limiting sustained for {mins}m"
            + (f" in {label}" if label else "")
            + "; streaming degraded.")
    notify = bot_config.notify_cmd()
    try:
        subprocess.run([notify, "--alert", "--tag", tag, body],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    log(f"429 storm alert fired after {mins}m in chat={TELEGRAM_CHAT_ID}")


def _take_too_long() -> bool:
    """Return True iff the most recent tg_api call failed with MESSAGE_TOO_LONG.
    Resets the flag, callers consume it once per check."""
    global _last_too_long
    out, _last_too_long = _last_too_long, False
    return out


def tg_api(method, params):
    """Ordering wrapper around _tg_api_raw: every sendMessage (a new
    message) goes out strictly in the order it was issued; see _seq_take.
    A marker job already holds its ticket (_seq_tls.held), so it doesn't
    take a second one."""
    if method != "sendMessage" or getattr(_seq_tls, "held", False):
        return _tg_api_raw(method, params)
    ticket = _seq_take()
    _seq_wait(ticket)
    try:
        return _tg_api_raw(method, params)
    finally:
        _seq_done(ticket)


def _tg_api_raw(method, params):
    """Call a Telegram Bot API method. Returns parsed dict or None on failure.

    NEVER blocks. Group chats burn through editMessageText rate limits fast;
    the old "time.sleep(retry_after)" path could pause this process for 30-
    40s per 429, during which claude's stdout pipe (65KB) fills up and
    claude's write() blocks, wedging the entire turn. Now: on 429 we set
    a cooldown deadline and return None for all calls until it passes.
    Telegram edits are idempotent (each paints the full current buffer), so
    a dropped streaming frame is harmless, the next successful flush
    catches up to the latest state.

    On 400 MESSAGE_TOO_LONG we additionally set _last_too_long so the
    streamer's recovery code knows to split the buffer rather than keep
    retrying. Other 400s (parse errors, not-modified) keep their existing
    handling.
    """
    global _rate_limited_until, _rate_limited_dropped, _last_too_long
    global _rl_storm_started, _rl_storm_alerted, _parse_entity_errs
    global _rl_backoff_level, _rl_clean_streak
    # Now called from two threads (the main loop's own blocking=True
    # end-of-turn commits, and the background sender for everything else).
    # _tg_api_lock protects the shared bookkeeping below, but deliberately
    # does NOT wrap the actual network call (see the urlopen call further
    # down, outside any `with _tg_api_lock` block): holding a lock across a
    # slow/blocked HTTP round-trip would mean any OTHER thread's call,
    # including the main thread's own blocking=True end-of-turn send,
    # has to wait for that one slow call to finish before it can even
    # check whether IT is allowed to proceed. Tried holding the lock for
    # the whole call first; test_streamer_no_backlog_block.py caught it
    # reintroducing the exact backlog bug this change fixes, just moved
    # from "waiting on cooldown" to "waiting on an unrelated in-flight
    # network call". The lock only needs to protect the shared counters
    # themselves, not the I/O.
    with _tg_api_lock:
        now = time.time()
        if now < _rate_limited_until:
            _rate_limited_dropped += 1
            return None
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API_URL}/{method}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            result = json.loads(r.read().decode())
        # Successful call: clear any in-progress 429 storm, and count
        # toward stepping the adaptive backoff back down (see
        # _rl_backoff_multiplier's docstring above).
        with _tg_api_lock:
            if _rl_storm_started > 0.0:
                _rl_storm_started = 0.0
                _rl_storm_alerted = False
            if _rl_backoff_level > 0:
                _rl_clean_streak += 1
                if _rl_clean_streak >= _RL_DECAY_STREAK:
                    _rl_backoff_level -= 1
                    _rl_clean_streak = 0
        return result
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
            j = json.loads(body)
        except Exception:
            j = None
        if j and e.code == 429:
            with _tg_api_lock:
                retry = (j.get("parameters") or {}).get("retry_after", 1)
                _rate_limited_until = now + retry + 0.2
                dropped_prev = _rate_limited_dropped
                _rate_limited_dropped = 0
                _rl_clean_streak = 0
                prev_level = _rl_backoff_level
                _rl_backoff_level = min(_rl_backoff_level + 1, _RL_BACKOFF_MAX)
                new_level = _rl_backoff_level
                # Storm tracking: alert once after 5 minutes of sustained 429s.
                fire_storm_alert = False
                if _rl_storm_started == 0.0:
                    _rl_storm_started = now
                elif not _rl_storm_alerted and now - _rl_storm_started >= _RL_STORM_THRESHOLD:
                    _rl_storm_alerted = True
                    fire_storm_alert = True
                    storm_duration = now - _rl_storm_started
            log(f"tg 429 retry_after={retry}; dropping streaming frames "
                f"for {retry}s (prev cycle dropped {dropped_prev}); "
                f"backoff level {prev_level}->{new_level} "
                f"(interval x{_rl_backoff_multiplier():.2g})")
            metrics.emit("tg_429", room=str(TELEGRAM_CHAT_ID),
                         retry_after=retry, backoff_level=new_level)
            if fire_storm_alert:
                _fire_429_storm_alert(storm_duration)
            return None
        if j and e.code == 400:
            desc = (j.get("description") or "").lower()
            if "not modified" in desc:
                return {"ok": True, "skipped": True}
            if "too_long" in desc or "message is too long" in desc:
                _last_too_long = True
            elif "can't parse entities" in desc or "can not parse entities" in desc:
                with _tg_api_lock:
                    _parse_entity_errs += 1
        log(f"tg api {method} error {e.code}: {body[:200]}")
        return None
    except Exception as e:
        log(f"tg api {method} net error: {e}")
        return None


def tg_send(text, keyboard=None):
    """Send a fresh message. Returns message_id or None.

    keyboard, when given, is a list of button rows (the same shape
    lib.sh's reply_kb builds in bash: [[{"text":..., "callback_data":...}]]),
    attached as an inline keyboard via reply_markup. Buttons use the same
    "act:<verb>" wire format as every other menu button in this bot
    (see CLAUDE.md's "Menu-first UX"), so the existing callback_query
    dispatch in listener.sh handles a tap with zero new plumbing.
    """
    params = {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "HTML",
        "text": html_format(text),
    }
    if keyboard:
        params["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    resp = tg_api("sendMessage", params)
    if resp and resp.get("ok") and "result" in resp:
        mid = resp["result"].get("message_id")
        _log_sent(mid)
        return mid
    return None


def tg_edit(message_id, text):
    """Edit an existing message. Returns True on success."""
    resp = tg_api("editMessageText", {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "parse_mode": "HTML",
        "text": html_format(text),
    })
    return bool(resp and resp.get("ok"))


# ── Native draft streaming (Telegram Bot API 9.5+ sendMessageDraft) ──────────
#
# In a DM, a draft renders as a smooth animated preview that the final real
# message seamlessly REPLACES, far nicer than editMessageText's stepwise
# repaint. We gate it to DMs (positive chat_id); groups/topics keep the
# edit-in-place path completely unchanged, so this can't regress them.
# Disable entirely with BOT_DRAFT_STREAMING=0.
#
# The one rule that makes the hand-off seamless: the final sendMessage text
# must be byte-identical to the last draft frame (same html_format output).
# Change so much as the first character and the client repaints the whole
# bubble, a visible "flash" at the end of the response. commit_live() sends
# exactly html_format(_buf), the same bytes the last draft frame carried.

def draft_mode():
    """Whether to use native draft streaming for THIS chat.

    Gated to DMs (positive chat_id). This is NOT a conservative choice, it's a
    hard Telegram limitation: sendMessageDraft against a group/supergroup
    chat_id is rejected with 400 TEXTDRAFT_PEER_INVALID (drafts are a
    peer-to-peer feature; a group is not a valid draft peer). Groups/topics
    (negative chat_id) therefore keep the editMessageText path. Even if this
    gate were bypassed, tg_send_draft would fail and the stream would fall
    back to edit, so it's belt-and-suspenders. Evaluated dynamically off the
    current TELEGRAM_CHAT_ID (not frozen at import) so it stays correct if the
    id is set late and so tests can flip it. BOT_DRAFT_STREAMING=0 disables it.
    """
    flag = os.environ.get("BOT_DRAFT_STREAMING", "auto").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    try:
        return int(TELEGRAM_CHAT_ID) > 0   # DMs/user chats only; groups are negative
    except (TypeError, ValueError):
        return False


DRAFT_LIVE = -1          # sentinel _live_id meaning "a draft box is live, no real msg yet"
try:
    DRAFT_INTERVAL = max(0.0, float(os.environ.get("BOT_DRAFT_INTERVAL", DRAFT_INTERVAL)))
except (TypeError, ValueError):
    pass
try:
    GROUP_EDIT_INTERVAL = max(0.0, float(os.environ.get("BOT_GROUP_EDIT_INTERVAL", GROUP_EDIT_INTERVAL)))
except (TypeError, ValueError):
    pass


def _edit_interval() -> float:
    try:
        is_group = int(TELEGRAM_CHAT_ID) < 0
    except (TypeError, ValueError):
        is_group = False
    return GROUP_EDIT_INTERVAL if is_group else EDIT_INTERVAL
_draft_id: int | None = None     # current draft's random id (one per live box)
_draft_failed = False    # a draft call hard-failed → fall back to edit for the rest
_draft_used_plain = False  # last shown draft frame fell back to plain text (no HTML)


def tg_send_draft(text):
    """Push one draft frame for the current live box. Returns True on success.

    Tries HTML first and falls back to plain text once if Telegram rejects the
    markup (it occasionally 400s a perfectly valid frame), mirroring the
    resilience of the send path so a single rejected frame can't kill draft
    streaming. Records whether the frame that actually landed was HTML or
    plain so commit_live() can finalise in the SAME format, otherwise a
    plain draft followed by an HTML final message would flash.
    """
    global _draft_id, _draft_used_plain
    if _draft_id is None:
        _draft_id = random.getrandbits(53)
    for use_html in (True, False):
        params = {
            "chat_id": TELEGRAM_CHAT_ID,
            "draft_id": _draft_id,
            "text": html_format(text) if use_html else text,
        }
        if use_html:
            params["parse_mode"] = "HTML"
        resp = tg_api("sendMessageDraft", params)
        if resp and resp.get("ok"):
            _draft_used_plain = not use_html
            return True
    return False


def tg_send_plain(text):
    """Send a fresh message with NO parse_mode (raw text). Used to finalise a
    draft that had to render as plain text, so the final message matches the
    last draft frame exactly. Returns message_id or None."""
    resp = tg_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
    if resp and resp.get("ok") and "result" in resp:
        mid = resp["result"].get("message_id")
        _log_sent(mid)
        return mid
    return None


def tg_clear_draft():
    """Clear the current draft (empty text). Best-effort, never raises."""
    if _draft_id is not None:
        tg_api("sendMessageDraft", {
            "chat_id": TELEGRAM_CHAT_ID, "draft_id": _draft_id, "text": "",
        })


# Module-level handles so the signal handler can flush before exit.
# main() writes through to these on every state change; nothing else does.
_live_id: int | None = None
_buf: str = ""

# Snapshot of _buf taken at the top of every commit_live() call, before any
# of its branches clear _buf to "". message_stop already commits+clears the
# live box, so by the time a later "result" event's is_error branch runs,
# _buf itself is long gone, this is what lets that branch see what was
# actually shown to the user a moment earlier (e.g. claude's own native
# rate-limit banner streamed as ordinary text).
_last_committed_text: str = ""

# Context % from the previous completed turn, used to detect compaction
# (a large drop between turns means auto-compact fired). Resets to None
# when the process starts (i.e. each new spawn gets a clean slate).
_prev_ctx_pct: float | None = None

# True while the live message contains ONLY the "💭 Thinking…" placeholder
# we emit on message_start. Cleared as soon as real output (text or a tool
# marker) arrives, mirroring Claude Code's "thinking indicator vanishes the
# moment work becomes visible" behaviour. commit_live() reads this to tear
# down the placeholder cleanly on tool-only / empty turns.
_showing_thinking: bool = False

# Per-turn verbosity snapshot, refreshed at every message_start by reading
# state/rooms/<chat>/verbosity. Cached for the turn so a setting change
# mid-stream doesn't half-apply (next turn picks up the change).
# Schema: {"tool_calls": "on|off", "edits": "diff|marker|off", "thinking": "on|off"}
_verbosity: dict = {"tool_calls": "on", "edits": "diff", "thinking": "on"}

# Tool names whose markers/diffs are governed by `verbosity.edits` (not by
# `verbosity.tool_calls`). Anything else falls under `verbosity.tool_calls`.
EDIT_TOOL_NAMES = ("Edit", "Write", "MultiEdit")

# Track tool_use_ids we've already rendered as separate Telegram messages
# so we don't double-emit when claude's stream sends the same envelope
# more than once.
_emitted_edits: set = set()


# Make the verbosity helper importable: this file lives in scripts/, same
# as verbosity.py, but the streamer is spawned with the repo root as cwd
# (or arbitrary, when launched via smoke tests). Insert scripts/ on
# sys.path so `import verbosity` resolves regardless of cwd.
sys.path.insert(0, str(REPO / "scripts"))
try:
    import verbosity as _verbosity_mod
except ImportError:
    _verbosity_mod = None


def _load_verbosity() -> dict:
    """Read the current room's verbosity dict (defaults on any error)."""
    if _verbosity_mod is None or not TELEGRAM_CHAT_ID:
        return dict(_verbosity_mod.DEFAULTS) if _verbosity_mod else {
            "tool_calls": "on", "edits": "diff", "thinking": "on"}
    try:
        return _verbosity_mod.load(REPO, TELEGRAM_CHAT_ID)
    except Exception:
        return dict(_verbosity_mod.DEFAULTS)


def _read_effort() -> str | None:
    """Return the per-room /effort level (low/medium/high/xhigh/max) or None.

    Reads state/rooms/<chat>/effort, the same file spawn_claude.sh consults
    to pick the --effort flag. Returns None if the file is absent or empty
    (meaning claude is using its default effort).
    """
    if not TELEGRAM_CHAT_ID:
        return None
    try:
        path = REPO / "state" / "rooms" / str(TELEGRAM_CHAT_ID) / "effort"
        if path.exists():
            txt = path.read_text().strip()
            return txt or None
    except Exception:
        pass
    return None

# In-progress tool_use blocks, keyed by content_block index. We accumulate
# `input_json` deltas here until content_block_stop, then render one
# activity line with both name AND a preview of the actual input.
_tool_blocks: dict = {}

# ScheduleWakeup inputs keyed by tool_use_id, so the matching tool_result
# can record the wakeup for listener.sh's missed-wakeup rescue. Claude Code
# skips every wakeup while any background task is still running, so one
# never-ending background wait silently kills a /loop (e.g. a stuck
# `until grep` starving every wakeup in a row). The streamer writes state/rooms/<chat>/.wakeup_due
# ({"due": epoch, "prompt": str}); the listener injects the prompt itself if
# the wakeup is well overdue and the room is sitting idle.
_wakeup_inputs: dict = {}


def _wakeup_file():
    return REPO / "state" / "rooms" / str(TELEGRAM_CHAT_ID) / ".wakeup_due"


def _note_wakeup_result(inp, text_out):
    """Track the room's pending wakeup from a ScheduleWakeup tool_result."""
    f = _wakeup_file()
    try:
        m = re.search(r"Next wakeup scheduled for .*?\(in (\d+)s\)", text_out)
        if m and not inp.get("stop"):
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps({"due": int(time.time()) + int(m.group(1)),
                                       "prompt": str(inp.get("prompt") or "")}))
            os.replace(tmp, f)
        elif "Loop stopped" in text_out or "Wakeup not scheduled" in text_out:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def _clear_wakeup_if_due():
    """A turn starting at or after the due time means the assistant is awake (the real
    wakeup fired, or a message woke it), so the rescue must stand down."""
    f = _wakeup_file()
    try:
        if f.exists() and time.time() >= json.loads(f.read_text()).get("due", 0):
            f.unlink(missing_ok=True)
    except Exception:
        pass


# Maps tool_use_id → {"mid": int, "text": str} for pending result-size
# annotation. Populated at content_block_stop for non-edit tools; consumed
# when the matching tool_result arrives in the user event.
_tool_call_mids: dict = {}

# Compact verbosity: tools listed by name in ONE message ("⚙️ Bash, ⚙️ Bash,
# 📝 Edit"), edited as more arrive. Closed by real text, a full marker or
# diff, ask_user's question, or the end of the turn; the next compact tool
# then starts a new message. Main thread only; the sends run on the marker
# queue (one worker, FIFO), so an append always lands after its group's
# first send and can read the mid that send stored.
_cgroup: dict | None = None     # {"items": [...], "mid": None}
COMPACT_MAX_CHARS = 3500        # start a new group well under 4096


def _compact_close():
    global _cgroup
    _cgroup = None


def _compact_add(item: str) -> None:
    """Add one tool to the open compact group, or open a new one."""
    global _cgroup
    g = _cgroup
    if g is not None and len(", ".join(g["items"] + [item])) > COMPACT_MAX_CHARS:
        g = None
    if g is None:
        g = _cgroup = {"items": [item], "mid": None}
        text = item

        def _send(g=g, text=text):
            g["mid"] = tg_send(text)
            g["shown"] = 1
        _bg_marker(_send)
        return
    g["items"].append(item)
    n = len(g["items"])

    def _edit(g=g, n=n):
        # A later append already queued will show more; skip this one.
        if not g.get("mid") or n < len(g["items"]) or g.get("shown", 0) >= n:
            return
        tg_edit(g["mid"], ", ".join(g["items"][:n]))
        g["shown"] = n
    _marker_queue.put(_edit)


def _compact_item(name: str, inp: dict | None = None) -> str:
    glyph, label = _tool_glyph_label(name, inp)
    return f"{glyph} {label}"


# One-line inline marker emoji for each tool. Anything not listed falls
# back to the wrench. The marker is appended to the live message buffer
# as a single line so the user sees chronological activity instead of an
# opaque "…" placeholder. Edit/Write/MultiEdit get a marker AND a
# separate diff message; everything else is marker-only.
TOOL_GLYPHS = {
    "Bash": "⚙️",
    "Read": "📖",
    "Edit": "📝",
    "Write": "📝",
    "MultiEdit": "📝",
    "Glob": "🔎",
    "Grep": "🔎",
    "WebFetch": "🌐",
    "WebSearch": "🔍",
    "Task": "🤖",
    "Agent": "🤖",
    "SendMessage": "💬",
    "ListAgents": "🤖",
    "Workflow": "🔀",
    "Skill": "🪄",
    "ToolSearch": "🧰",
    "TodoWrite": "✅",
    "TaskCreate": "✅",
    "TaskUpdate": "✅",
    "TaskList": "✅",
    "TaskGet": "✅",
    "TaskOutput": "📤",
    "TaskStop": "🛑",
    "ScheduleWakeup": "⏰",
    "CronCreate": "⏰",
    "CronList": "⏰",
    "CronDelete": "⏰",
    "Monitor": "👁",
    "PushNotification": "🔔",
    "RemoteTrigger": "📡",
    "EnterWorktree": "🌳",
    "ExitWorktree": "🌳",
    "ReportFindings": "🧾",
    "DesignSync": "🎨",
    "NotebookEdit": "📓",
    "BashOutput": "⚙️",
    "KillShell": "⚙️",
    "ask_user": "❓",
    # Browser MCP plugin (e.g. chromeflow) short names, looked up after
    # stripping the MCP prefix.
    "open_page": "🌐",
    "click_element": "🖱",
    "click_at_coordinates": "🖱",
    "wait_for_click": "🖱",
    "type_text": "⌨️",
    "fill_input": "⌨️",
    "fill_form": "⌨️",
    "find_text": "🔎",
    "get_page_text": "📄",
    "get_page_html": "📄",
    "execute_script": "🧪",
    "switch_to_tab": "🗂",
    "list_tabs": "🗂",
    "list_frames": "🗂",
    "close_tab": "🗂",
    "close_other_tabs": "🗂",
    "take_screenshot": "📸",
    "interactive_snapshot": "📸",
    "capture_terminal": "💻",
    "set_file_input": "📎",
    "read_attachment": "📎",
    "download_file": "⬇️",
    "fetch_url": "🌐",
    "scroll_to_element": "📜",
    "scroll_page": "📜",
    "get_form_fields": "📋",
    "get_console_logs": "🔍",
    "inspect_request_headers": "🔍",
    "wait_for": "⏳",
    "highlight_region": "🎯",
    "clear_overlays": "🧹",
    "write_to_env": "⚙️",
    "save_flow": "💾",
}

_MCP_CHROMEFLOW_PREFIX = "mcp__plugin_chromeflow_chromeflow__"

# Unknown tools: 🔌 for any MCP server's tool, 🛠 for anything else.
MCP_GLYPH = "🔌"
OTHER_GLYPH = "🛠"
MEMORY_GLYPH = "🧠"


def _is_memory_path(path) -> bool:
    """Claude Code's memory files live in ~/.claude/.../memory/."""
    p = str(path or "")
    return "/.claude/" in p and "/memory/" in p


def _tool_glyph_label(name: str, inp: dict | None = None):
    """(glyph, label) for a tool call. Reading or writing a memory file
    shows as 🧠 Memory rather than a plain Read/Edit/Write."""
    short = _short_tool_name(name)
    if (short in ("Read", "Edit", "Write", "MultiEdit")
            and _is_memory_path((inp or {}).get("file_path"))):
        return MEMORY_GLYPH, "Memory"
    glyph = (TOOL_GLYPHS.get(name) or TOOL_GLYPHS.get(short)
             or (MCP_GLYPH if name.startswith("mcp__") else OTHER_GLYPH))
    return glyph, short


def _short_tool_name(name: str) -> str:
    """Compact display name. Strips the browser MCP plugin prefix (e.g.
    chromeflow's) so we show e.g. "click_element" instead of the full
    mcp__... mouthful."""
    if name.startswith(_MCP_CHROMEFLOW_PREFIX):
        return name[len(_MCP_CHROMEFLOW_PREFIX):]
    if name.startswith("mcp__"):
        # Generic MCP tool: drop the "mcp__<server>__<server>__" boilerplate.
        rest = name[5:]
        parts = rest.split("__", 2)
        return parts[-1] if parts else name
    return name


def _tool_preview(name: str, inp: dict) -> str:
    """One-line ≤72-char summary of a tool call's input. Returns "" when we
    can't surface anything useful (input not yet populated, unknown tool).
    Pure formatting, never raises.
    """
    if not isinstance(inp, dict) or not inp:
        return ""
    short = _short_tool_name(name)

    def first_line(s, n=72):
        return (str(s) or "").replace("\n", " ⏎ ").replace("\r", " ")[:n]

    # Native Claude Code tools
    if name == "Bash":
        return first_line(inp.get("command") or inp.get("description") or "")
    if name == "BashOutput":
        return first_line(inp.get("bash_id") or "")
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        fp = inp.get("file_path") or ""
        parts = fp.split("/")
        return first_line("/".join(parts[-3:]) if len(parts) > 3 else fp)
    if name in ("Grep", "Glob"):
        return first_line(inp.get("pattern") or "")
    if name in ("WebFetch", "WebSearch"):
        return first_line(inp.get("url") or inp.get("query") or "")
    if name in ("Task", "Agent"):
        return first_line(inp.get("description") or inp.get("subject") or "")
    if name in ("TaskCreate", "TaskUpdate"):
        return first_line(inp.get("subject") or inp.get("status")
                          or inp.get("taskId") or "")
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return f"{len(todos)} todo(s)" if todos else ""
    if name == "ToolSearch":
        return first_line(inp.get("query") or "")

    # Browser MCP plugin (e.g. chromeflow) shortcuts
    if short == "open_page":
        return first_line(inp.get("url") or "")
    if short == "click_element":
        return first_line(inp.get("textHint") or inp.get("selector") or "")
    if short == "fill_input":
        sel = first_line(inp.get("textHint") or inp.get("selector") or "", 36)
        val = first_line(inp.get("value") or "", 32)
        return f"{sel} ← {val}" if sel or val else ""
    if short == "fill_form":
        return first_line(inp.get("form_selector") or inp.get("selector") or "")
    if short == "type_text":
        return first_line(inp.get("text") or "")
    if short == "find_text":
        return first_line(inp.get("query") or "")
    if short == "switch_to_tab":
        return first_line(inp.get("tab") or inp.get("query") or "")
    if short in ("close_tab", "close_other_tabs"):
        return first_line(inp.get("tab") or inp.get("query") or "")
    if short == "execute_script":
        return first_line(inp.get("script") or inp.get("code") or "")
    if short == "get_page_text":
        bits = []
        if inp.get("startIndex"):
            bits.append(f"startIndex={inp['startIndex']}")
        if inp.get("selector"):
            bits.append(f"selector={inp['selector']}")
        return first_line(", ".join(bits))
    if short in ("take_screenshot", "interactive_snapshot", "capture_terminal"):
        return first_line(inp.get("save_to") or inp.get("selector") or "")
    if short == "set_file_input":
        hint = first_line(inp.get("hint") or inp.get("textHint") or "", 30)
        fp = inp.get("file_path") or inp.get("path") or ""
        fname = fp.split("/")[-1] if fp else ""
        return f"{hint} ← {fname}" if hint else fname
    if short in ("fetch_url", "download_file"):
        return first_line(inp.get("url") or inp.get("path") or "")
    if short == "scroll_to_element":
        return first_line(inp.get("selector") or inp.get("textHint") or "")
    if short == "get_form_fields":
        return first_line(inp.get("selector") or inp.get("form_selector") or "")
    if short in ("wait_for", "wait_for_click"):
        return first_line(inp.get("condition") or inp.get("selector") or inp.get("textHint") or "")
    if short in ("get_page_html", "get_console_logs", "inspect_request_headers"):
        return first_line(inp.get("selector") or inp.get("url") or "")
    if short == "highlight_region":
        return first_line(inp.get("selector") or "")
    if short == "write_to_env":
        return first_line(inp.get("key") or next(iter(inp), "") or "")
    if short == "save_flow":
        return first_line(inp.get("label") or inp.get("name") or "")

    # Generic fallback: first 1-2 key=value pairs.
    items = list(inp.items())[:2]
    return first_line(", ".join(f"{k}={str(v)[:30]}" for k, v in items))


def _tool_line(name: str, inp: dict | None = None) -> str:
    """Render a tool-call activity line for the live message buffer.

    Format (two-line when there's a preview, single-line otherwise):
        {glyph} *{short_name}*
        `{preview}`

    Putting the args on their own line stops the bold tool name from
    visually merging with a long Bash command on phone-width wraps, and
    gives the eye a clean break between consecutive tool calls. The
    preview ships inside Telegram backticks so it renders as inline code
    special chars (quotes, brackets, &) don't have to be escaped here,
    md_to_telegram_html handles the rest.
    """
    glyph, short = _tool_glyph_label(name, inp)
    preview = _tool_preview(name, inp or {})
    if preview:
        return f"{glyph} *{short}*\n`{preview}`"
    return f"{glyph} *{short}*"


def _neutralize_fences(s: str) -> str:
    """Break up any literal run of 3+ backticks inside untrusted content
    (file bodies, old_string/new_string) so it can't be mistaken for our
    own outer ``` fence delimiter.

    Why: _format_edit_tool wraps a Write's whole file content (or
    an Edit's old/new strings) in one outer ``` fence. If that content
    already contains its own ``` (any markdown file with a fenced example
    block, which is extremely common in docs), md_telegram.py's
    _FENCED_RE pairs the four resulting backtick runs in encounter order
    (1st-with-2nd, 3rd-with-4th) instead of open-with-matching-close, so
    the middle of the message falls OUTSIDE any code block and renders as
    plain text while the tail gets wrongly wrapped in its own <pre>.
    Inserting a zero-width space (U+200B) after the first backtick of
    every 3+ run defeats that regex match while staying visually
    identical to the reader, no other code path needs to change.
    """
    return re.sub(r"`{3,}", lambda m: "`​" + m.group(0)[1:], s)


def _format_edit_tool(name: str, inp: dict) -> str | None:
    """Build a Telegram message string for an Edit/Write/MultiEdit call.

    Layout:
        📝 *name* `path/to/file.py`

        ```
        -̶ ̶r̶e̶m̶o̶v̶e̶d̶ ̶l̶i̶n̶e̶
        + added line
        ```

    The diff body sits inside a markdown fenced code block (becomes Telegram's
    `<pre>`, monospace, edges visible on every client). Removed lines carry a
    Unicode U+0336 combining-stroke on every character (so they render struck
    through INSIDE the monospace block, which `<s>` cannot do, Telegram
    refuses formatting tags inside `<pre>`). Added lines are plain `+ ...`.

    Truncates long content so we stay under Telegram's 4096 cap.
    """
    fp = inp.get("file_path") or ""
    if not fp:
        return None
    # Compact the path: keep last 3 components.
    parts = fp.split("/")
    short_path = "/".join(parts[-3:]) if len(parts) > 3 else fp

    MAX_LINES_OLD = 12
    MAX_LINES_NEW = 12
    MAX_CHARS_PER_LINE = 200

    def trim_block(s: str, max_lines: int) -> str:
        lines = s.split("\n")
        if len(lines) > max_lines:
            keep_head = max_lines - 2
            head = lines[:keep_head]
            tail_count = len(lines) - keep_head
            return ("\n".join(t[:MAX_CHARS_PER_LINE] for t in head) +
                    f"\n… ({tail_count} more lines)")
        return "\n".join(t[:MAX_CHARS_PER_LINE] for t in lines)

    STRIKE = "̶"   # COMBINING LONG STROKE OVERLAY

    def struck(line: str) -> str:
        """Apply combining stroke to each char so the line renders struck
        through inside a `<pre>` block (where `<s>` tags don't render)."""
        # Skip strokes on the line's leading "- " prefix? No, striking it
        # too keeps the visual consistent and is what the user expects from
        # a deleted line in a diff.
        return "".join(c + STRIKE for c in f"- {line}")

    glyph = MEMORY_GLYPH if _is_memory_path(inp.get("file_path")) else "📝"
    body_parts = [f"{glyph} *{name}* `{short_path}`", ""]

    if name == "Write":
        # Full new content: no diff, just the new body in a code block.
        content = _neutralize_fences(inp.get("content") or "")
        body_parts.append("```")
        body_parts.append(trim_block(content, MAX_LINES_NEW + 6))
        body_parts.append("```")
    elif name == "Edit":
        old = _neutralize_fences(inp.get("old_string") or "")
        new = _neutralize_fences(inp.get("new_string") or "")
        body_parts.append("```")
        for line in trim_block(old, MAX_LINES_OLD).split("\n"):
            body_parts.append(struck(line))
        for line in trim_block(new, MAX_LINES_NEW).split("\n"):
            body_parts.append(f"+ {line}")
        body_parts.append("```")
    elif name == "MultiEdit":
        edits = inp.get("edits") or []
        body_parts.append(f"_{len(edits)} edits_")
        for i, e in enumerate(edits[:3]):
            old = _neutralize_fences((e.get("old_string") or "").split("\n")[0][:80])
            new = _neutralize_fences((e.get("new_string") or "").split("\n")[0][:80])
            body_parts.append("```")
            body_parts.append(struck(old))
            body_parts.append(f"+ {new}")
            body_parts.append("```")
        if len(edits) > 3:
            body_parts.append(f"… and {len(edits) - 3} more edits")

    text = "\n".join(body_parts)
    # Hard cap. Close any open fence first so the truncation marker isn't
    # rendered inside a runaway monospace block, then append the marker.
    if len(text) > 3700:
        text = text[:3700] + "\n```\n…(truncated)"
    return text


def _cancel_handler(signum, frame):
    """On SIGTERM/SIGINT, decide whether the kill is user-initiated (real
    /cancel) or just the parent restarting (launchctl unload, dev reloads).

    The distinguishing signal: handle_cancel_inline (listener.sh) writes
    a marker file `state/rooms/<chat>/.cancel_requested` BEFORE
    SIGTERM-ing the spawn. If that marker is present we mark the live
    message; otherwise we exit silently so a routine listener reload
    doesn't paint "cancelled" on a perfectly healthy turn.

    EITHER WAY: we must tear down a live "Thinking..." placeholder before
    exiting. Otherwise the placeholder hangs in the chat forever and never
    clears. The placeholder is the
    only state where _showing_thinking is True; in every other case the
    live buffer has real content and gets a "cancelled"/leave-as-is
    treatment instead.
    """
    # This handler interrupts the main thread, which may itself be parked in
    # _seq_wait holding an earlier ticket; a new ticket here would wait on it
    # forever (bounded, but 25s). Order no longer matters once cancelling.
    with _seq_cv:
        _seq_bypass[0] = True
        _seq_cv.notify_all()
    try:
        chat = TELEGRAM_CHAT_ID
        cancel_marker_was_present = False
        if chat:
            marker = REPO / "state" / "rooms" / str(chat) / ".cancel_requested"
            if marker.exists():
                cancel_marker_was_present = True
                try:
                    marker.unlink()
                except Exception:
                    pass

        if _showing_thinking and _live_id is not None:
            # Always remove the thinking placeholder on any signal-exit,
            # whether the user cancelled or the parent restarted. Leaving
            # it in place is the visible bug.
            if _live_id == DRAFT_LIVE:
                tg_clear_draft()
            else:
                tg_api("deleteMessage", {
                    "chat_id": TELEGRAM_CHAT_ID, "message_id": _live_id,
                })
        elif cancel_marker_was_present and _live_id is not None and _buf:
            # Real content was streaming when the user pressed cancel.
            # Paint the cancellation so they see why it stopped.
            if _live_id == DRAFT_LIVE:
                tg_clear_draft()
                tg_send(_buf + "\n\n🛑 cancelled")
            else:
                tg_edit(_live_id, _buf + "\n\n🛑 cancelled")
    except Exception:
        pass
    sys.exit(130)


signal.signal(signal.SIGTERM, _cancel_handler)
signal.signal(signal.SIGINT, _cancel_handler)


# ── Inbox read tracking ────────────────────────────────────────────────────
# spawn_claude.sh passes --replay-user-messages, so Claude Code echoes every
# inbox line back (a "user" event with isReplay) the moment it reads it,
# including lines it folds into a turn that is already running. Matching
# those echoes against inbox.ndjson tells us exactly which lines have been
# read, so reactions and the crash salvage no longer assume "one inbox line
# per result" (false since Claude Code started reading stdin mid-turn: a
# message sent mid-turn was folded in and answered, but its reaction stayed
# 🫡 because the next result popped the wrong .pending_reacts entry).
# .pending_reacts stays line-for-line parallel with inbox.ndjson (see
# listener.sh's inject_to_inbox); it is indexed, never popped.
_inbox_cursor = 0                  # inbox lines read so far this spawn
_turn_lines: list = []             # inbox line indexes read this turn
_replay_seen = False               # has this claude ever echoed a line?
_ptl_compact_queued = False        # recovery /compact appended, not yet run
_ptl_retried: set = set()          # contents already retried once
_compact_mid = None                # "🗜 Compressing context…" bubble
_compact_started = 0.0
_in_turn = False
_compact_announced = False         # bubble showed "Context compressed"

PTL_MARKERS = ("prompt is too long", "prompt too long", "context length")


def _room_path(name: str) -> Path:
    return REPO / "state" / "rooms" / str(TELEGRAM_CHAT_ID) / name


def _norm_content(content) -> str:
    return json.dumps(content, sort_keys=True, ensure_ascii=False)


def _inbox_entries() -> list:
    """inbox.ndjson as parsed dicts, one per line ({} for unparseable)."""
    try:
        raw = _room_path("inbox.ndjson").read_text().splitlines()
    except Exception:
        return []
    out = []
    for line in raw:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            out.append(obj if isinstance(obj, dict) else {})
        except Exception:
            out.append({})
    return out


def _entry_content(obj: dict):
    return (obj.get("message") or {}).get("content")


def _is_user_entry(obj: dict) -> bool:
    return obj.get("type") == "user"


def _pending_ids(idx: int) -> list:
    try:
        lines = [l for l in _room_path(".pending_reacts").read_text().splitlines() if l.strip()]
        ids = json.loads(lines[idx]) if idx < len(lines) else []
        return [i for i in ids if isinstance(i, int)]
    except Exception:
        return []


def _react(ids, emoji: str):
    for mid in ids:
        try:
            tg_api("setMessageReaction", {
                "chat_id": str(TELEGRAM_CHAT_ID),
                "message_id": str(mid),
                "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
            })
        except Exception:
            pass


def _write_read_count():
    """Lines fully handled, for spawn_claude.sh's crash salvage."""
    try:
        _room_path(".turns_done").write_text(str(_inbox_cursor))
    except Exception:
        pass


def _note_replay(content) -> bool:
    """Claude just read an inbox line: find it at or after the cursor.
    Lines it passes over (control requests, an unechoed /compact) count as
    read too. Echoes that match no line (the compaction summary, local
    command output) are ignored."""
    global _inbox_cursor, _replay_seen, _in_turn
    want = _norm_content(content)
    entries = _inbox_entries()
    for i in range(_inbox_cursor, len(entries)):
        if _is_user_entry(entries[i]) and _norm_content(_entry_content(entries[i])) == want:
            _replay_seen = True
            _in_turn = True
            new = list(range(_inbox_cursor, i + 1))
            _turn_lines.extend(new)
            _inbox_cursor = i + 1
            try:   # commands/cancel.sh's force-stop keeps lines after this
                _room_path(".inbox_read").write_text(str(_inbox_cursor))
            except Exception:
                pass
            ids = [m for n in new for m in _pending_ids(n)]
            _react(ids, "👀")          # read by Claude, answer on its way
            return True
    return False


def _mark_in_turn():
    global _in_turn
    _in_turn = True


def _close_turn(ev: dict) -> list:
    """Return the inbox line indexes this result answered, advancing the
    cursor when no echo was seen (older Claude Code, the e2e fake, or a
    /compact line, which is never echoed)."""
    global _inbox_cursor, _in_turn
    _in_turn = False
    lines = list(_turn_lines)
    _turn_lines.clear()
    if lines:
        return lines
    entries = _inbox_entries()
    # Step over control lines (interrupt requests) at the cursor.
    while _inbox_cursor < len(entries) and not _is_user_entry(entries[_inbox_cursor]):
        _inbox_cursor += 1
    if _inbox_cursor >= len(entries):
        return []
    at_cursor = _entry_content(entries[_inbox_cursor])
    if (not _replay_seen and not ev.get("is_error")) or at_cursor == "/compact":
        _inbox_cursor += 1
        return [_inbox_cursor - 1]
    if ev.get("is_error") and any(k in str(ev.get("result") or "").lower() for k in PTL_MARKERS):
        # Rejected before Claude echoed it: the failed message is the next line.
        _inbox_cursor += 1
        return [_inbox_cursor - 1]
    return []


def _append_inbox(content, ids):
    """Append one user line to the inbox with its parallel reacts line."""
    with open(_room_path("inbox.ndjson"), "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
    with open(_room_path(".pending_reacts"), "a") as f:
        f.write(json.dumps(list(ids)) + "\n")


def _recover_prompt_too_long(lines: list) -> bool:
    """The context was too full to take this message. Queue /compact and
    then the same message(s) again, once. Returns False when the message
    was already retried (it is too big even after compressing)."""
    global _ptl_compact_queued
    entries = _inbox_entries()
    retry = []
    for i in lines:
        if i < len(entries) and _is_user_entry(entries[i]):
            c = _entry_content(entries[i])
            if c != "/compact":
                retry.append((c, _pending_ids(i)))
    if any(_norm_content(c) in _ptl_retried for c, _ in retry):
        return False
    try:
        if not _ptl_compact_queued:
            _append_inbox("/compact", [])
            _ptl_compact_queued = True
        for c, ids in retry:
            _ptl_retried.add(_norm_content(c))
            _append_inbox(c, ids)
    except Exception as e:
        log(f"prompt-too-long recovery failed: {e}")
        return False
    return True


def _record_ctx(ev: dict):
    """Leave the context fill for listener.sh's context_full, which
    compresses BEFORE delivering the next message once it is high."""
    pct = ctx_pct_from_result(ev)
    if pct is None:
        return
    try:
        _room_path(".ctx_pct").write_text(f"{pct:.0f}")
    except Exception:
        pass


def _compact_threshold() -> int:
    """The room's auto compact setting, else BOT_PRECOMPACT_PCT (90)."""
    try:
        v = int(_room_path("autocompact").read_text().strip())
        if 40 <= v <= 90:
            return v
    except Exception:
        pass
    try:
        return int(bot_config.get("BOT_PRECOMPACT_PCT", "90") or 90)
    except ValueError:
        return 90


def _compact_now_if_full(ev: dict):
    """A reply that ends with the context at or past the threshold queues a
    /compact straight away, so it runs while the user reads the answer
    instead of in front of their next message. Only when nothing unread is
    waiting (that message would otherwise be answered first, at the full
    context; listener.sh's context_full and the prompt-too-long recovery
    cover it)."""
    global _ptl_compact_queued
    if ev.get("is_error") or _ptl_compact_queued:
        return
    pct = ctx_pct_from_result(ev)
    if pct is None or pct < _compact_threshold():
        return
    if _inbox_cursor < len(_inbox_entries()):
        return
    try:
        _append_inbox("/compact", [])
        _ptl_compact_queued = True
        _room_path(".ctx_pct").unlink(missing_ok=True)
        # Marks the next result as the compaction: no "Worked for" footer,
        # the progress bubble is the confirmation.
        _room_path(".compact_in_flight").touch()
        log(f"context at {pct:.0f}%, compacting now")
    except Exception as e:
        log(f"auto compact failed: {e}")


def _compacting_marker(on: bool):
    try:
        p = _room_path(".compacting")
        if on:
            p.touch()
        else:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def _on_system(ev: dict):
    """Show compaction as it happens: Claude Code can spend minutes
    summarising, during which nothing else streams and new messages sit
    unanswered, so without this the chat just looks dead."""
    global _compact_mid, _compact_started, _ptl_compact_queued, _compact_announced
    sub = ev.get("subtype")
    if sub == "status" and ev.get("status") == "compacting":
        if _compact_mid is None:
            _compact_started = time.monotonic()
            _compacting_marker(True)
            _compact_mid = tg_send("🗜 _Compressing context, messages you send now will be answered once it finishes…_") or 0
    elif sub == "status" and ev.get("compact_result"):
        ok = ev.get("compact_result") == "success"
        if not ok and _compact_mid:
            tg_edit(_compact_mid, "⚠️ Context compression failed.")
        if not ok:
            _compact_mid = None
            _compacting_marker(False)
            _ptl_compact_queued = False
    elif sub == "compact_boundary":
        meta = ev.get("compact_metadata") or {}
        pre, post = meta.get("pre_tokens"), meta.get("post_tokens")
        secs = int(time.monotonic() - _compact_started) if _compact_started else 0
        sizes = f": {pre // 1000}k → {post // 1000}k tokens" if pre and post else ""
        text = f"🗜 Context compressed{sizes} in {fmt_duration(secs * 1000)}."
        if _compact_mid:
            tg_edit(_compact_mid, text)
        elif _compact_mid is None:
            tg_send(text)
        _compact_mid = None
        _compact_started = 0.0
        _compact_announced = True
        _room_path(".ctx_pct").unlink(missing_ok=True)
        _compacting_marker(False)
        _ptl_compact_queued = False


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        sys.stderr.write("stream_to_telegram: missing TELEGRAM_BOT_TOKEN/CHAT_ID\n")
        sys.exit(2)

    # live_id / buf are module-level globals (declared at top) so the
    # SIGTERM handler can read them without us mirroring through a dict
    # on every state change. Everything else stays local to main().
    global _live_id, _buf, _showing_thinking, _verbosity
    _live_id = None
    _buf = ""
    _showing_thinking = False
    _verbosity = _load_verbosity()
    last_edit = 0.0
    finalized = False          # message_stop / result has fired → no more flushes
    saw_text = False           # did we ever stream any text?
    thinking_sep_pending = False  # new thinking block: separate its first visible chunk
    auth_alert_sent = False    # one-shot guard so we don't spam 401 alerts

    def check_auth_failure(text):
        """If `text` looks like a claude auth failure (401 / Invalid creds),
        post a loud actionable alert to Telegram exactly once per spawn.

        Claude reports auth errors as plain assistant text like
        "Failed to authenticate. API Error: 401 Invalid authentication
        credentials" rather than as a structured is_error event, so we
        have to grep the text. Catches both the OAuth-expired case (the
        bot's CLAUDE_CODE_OAUTH_TOKEN went stale) and the rarer 'Invalid
        API key' that fires when ANTHROPIC_API_KEY leaks into the env.

        NOTE: the broad ("401" in text AND "auth" in text) check was
        removed, it false-positived whenever Claude discussed HTTP auth
        in any codebase (any API, any project). The three specific
        phrases below are sufficient: they all appear verbatim in the
        CLI's own error output and are unlikely to arise in normal
        conversation.
        """
        nonlocal auth_alert_sent
        if auth_alert_sent or not text:
            return
        low = text.lower()
        if "invalid authentication credentials" in low or \
           "failed to authenticate" in low or \
           "invalid api key" in low:
            auth_alert_sent = True
            tg_send(
                "🚨 *Bot OAuth token expired*\n\n"
                "Claude returned 401: the token in `.env` is no longer valid.\n\n"
                "*Fix from your phone (~1 min):*\n"
                "Open the menu and tap *🔐 login*. The bot will send you a "
                "tap-to-authorize link, capture the new token, and restart "
                "itself automatically.\n\n"
                "Send `/menu` if you don't have a menu in this chat already."
            )

    def ensure_live():
        global _live_id, _draft_id, _draft_used_plain
        nonlocal last_edit
        if _live_id is None:
            if draft_mode() and not _draft_failed:
                # No real message yet: open a draft box. The first frame goes
                # out on the next flush (last_edit=0 lets it fire immediately).
                _draft_id = random.getrandbits(53)
                _draft_used_plain = False
                _live_id = DRAFT_LIVE
                last_edit = 0.0
            else:
                mid = tg_send("…")
                if mid is not None:
                    _live_id = mid
                    last_edit = time.monotonic()
        return _live_id is not None

    def _fallback_to_edit():
        """A draft frame hard-failed; salvage by switching this response to the
        edit path (create a real message with the buffer so far)."""
        global _live_id, _draft_id, _draft_failed
        nonlocal last_edit
        _draft_failed = True
        tg_clear_draft()
        _draft_id = None
        mid = tg_send(_buf if _buf else "…")
        if mid is None:
            mid = tg_send_plain(_buf if _buf else "…")
        _live_id = mid if mid is not None else None
        last_edit = time.monotonic()

    def force_split_rotate():
        """Recover from MESSAGE_TOO_LONG: split _buf at a safe html-length
        boundary, commit the head into the current live box (which now
        fits), and open a new message for the tail. Continues the stream
        in the new box. Used when an edit/send 400s with MESSAGE_TOO_LONG
        despite the proactive html-length check, happens with mid-message
        markdown constructs whose expansion crosses the line.
        """
        global _live_id, _buf, _draft_id
        nonlocal last_edit
        head, tail = _safe_split(_buf, MAX_HTML_CHARS)
        # Commit the head into whatever box is currently live.
        if _live_id == DRAFT_LIVE:
            # Draft path: send head as a real message (the draft was the
            # too-long one; clear it so the client doesn't keep animating).
            if head:
                tg_send(head)
            tg_clear_draft()
            _draft_id = None
        elif _live_id is not None and head:
            # Edit path: head should now fit. If it doesn't (Telegram still
            # 400s), bail to a fresh send so we make forward progress.
            if not tg_edit(_live_id, head):
                if tg_send(head) is None:
                    tg_send_plain(head)
        _live_id = None
        _buf = ""
        last_edit = 0.0
        # Open a fresh live box for the tail and keep streaming.
        if not tail:
            return
        if draft_mode() and not _draft_failed:
            _buf = tail
            ensure_live()
            maybe_flush(force=True)
        else:
            mid = tg_send(tail)
            if mid is not None:
                _live_id = mid
                _buf = tail
                last_edit = time.monotonic()

    # Cross-thread recovery signal for maybe_flush's backgrounded sends
    # below. force_split_rotate()/_fallback_to_edit() mutate _live_id/_buf/
    # _draft_id, which only the MAIN thread may safely touch (it's also
    # the thread appending to _buf as new content_block_delta events
    # arrive), so a background job that hits MESSAGE_TOO_LONG or a hard
    # draft failure can't fix it itself. It just raises a flag; the main
    # thread handles it at the top of the very next maybe_flush() call,
    # which follows within a fraction of a second in practice since
    # content_block_delta fires continuously while text is streaming.
    # (Belt and braces: commit_live()'s own too-long check independently
    # catches the same oversized buffer on its own synchronous attempt if
    # this flag somehow goes unconsumed, e.g. generation stops right after.)
    _pending_recovery = [None]  # "too_long" | "draft_failed" | None

    def maybe_flush(force=False):
        nonlocal last_edit
        if _pending_recovery[0] == "too_long":
            _pending_recovery[0] = None
            force_split_rotate()
            return
        if _pending_recovery[0] == "draft_failed":
            _pending_recovery[0] = None
            _fallback_to_edit()
            return
        if _live_id is None or not _buf:
            return
        now = time.monotonic()
        # Draft frames flush faster than full edits (lighter, streaming API).
        # Scaled by the adaptive 429 backoff multiplier (see its docstring):
        # a room that's been tripping Telegram's flood limit gets a wider
        # gap here until it earns its way back down via clean calls.
        interval = (DRAFT_INTERVAL if _live_id == DRAFT_LIVE else _edit_interval()) \
            * _rl_backoff_multiplier()
        if not (force or (now - last_edit) >= interval):
            return
        # This used to call tg_send_draft/tg_edit directly and block this
        # whole loop on the network round-trip. Under heavy streaming that
        # cost compounds across many calls until the loop falls behind
        # claude's own output; see the _send_queue comment near the top
        # of this file for the full story and a reproduced example.
        # Snapshot the target now; the real send happens on the background
        # sender thread so THIS loop can immediately go back to reading
        # claude's next line. Update last_edit optimistically (on dispatch,
        # not confirmed success) so a slow/in-flight send doesn't cause the
        # same content to be re-enqueued on every subsequent delta.
        last_edit = now
        buf_snapshot = _buf
        live_id_snapshot = _live_id
        # Counted so commit_live() can wait for just THIS job (not the
        # whole queue) before it touches _draft_id/_draft_used_plain, see
        # _wait_for_flush_jobs's own docstring above.
        _flush_job_started()
        if live_id_snapshot == DRAFT_LIVE:
            def _job(buf=buf_snapshot):
                try:
                    if tg_send_draft(buf):
                        return
                    if _take_too_long():
                        _pending_recovery[0] = "too_long"
                    else:
                        _pending_recovery[0] = "draft_failed"
                finally:
                    _flush_job_finished()
            _bg_edit(_job)
        else:
            def _job(mid=live_id_snapshot, buf=buf_snapshot):
                try:
                    if tg_edit(mid, buf):
                        return
                    if _take_too_long():
                        _pending_recovery[0] = "too_long"
                finally:
                    _flush_job_finished()
            _bg_edit(_job)

    def commit_live(blocking=True):
        """Finalise the current live box and reset live state.

        Edit mode: a final flush in place (the edited message IS the result).
        Draft mode: replace the streamed draft with a real, byte-identical
        sendMessage (so the client doesn't repaint → no flash) and clear the
        draft. After this the next box opens fresh (new draft id).

        Tool-only / empty turns: if the live box is still just the
        "💭 Thinking…" placeholder (no real text or tool markers ever
        landed), tear it down instead of committing it as a real message,
        Claude Code's behaviour is that the thinking indicator vanishes when
        work begins or finishes, never persisting as a stranded line.

        blocking=True (message_stop / result / end-of-stdin / prior-turn
        cleanup): genuinely end-of-stream, so waiting out an active 429
        cooldown (up to COOLDOWN_WAIT_MAX) before giving up is correct,
        the alternative is losing the reply outright.

        blocking=False (tool_use content_block_stop, mid-stream text
        overflow in rotate_message, the "diff" verbosity edit-render path):
        this call is firing MID-TURN, with more text/tool markers still to
        come in the SAME turn. Previously every one of these mid-turn call
        sites used to run the SAME unconditional
        _wait_for_cooldown() as the true end-of-turn case, which blocks for
        up to 90s. logs/stream.log shows this firing often under normal
        load, with frequent multi-second waits.
        Claude's own tool execution (e.g. a Bash call to notify-cmd/tg.sh
        sending a photo) is NOT gated on the streamer at all, it runs
        immediately, independent of whatever stream_to_telegram.py is doing
        downstream, so the photo lands in seconds while the pre-tool
        narration and tool marker sat blocked behind a cooldown wait that
        has nothing to do with the photo's own delivery, arriving visibly
        AFTER a file that logically should have preceded it. Skipping the
        wait (and the failure-escalation notices below, which are worded
        for a genuinely final delivery) means a dropped mid-turn segment
        just doesn't show as its own bubble, the tool marker and
        subsequent text still arrive normally in a fresh box, exactly the
        same "drop is fine, the next flush catches up" tolerance the
        ordinary streaming edit path already has. Also honours `tg_api`'s
        own docstring warning (the same reason the footer's cooldown wait
        is scoped to end-of-turn only): blocking mid-turn risks claude's
        own stdout pipe filling and wedging.
        """
        global _live_id, _buf, _draft_id, _showing_thinking, _last_committed_text
        nonlocal last_edit
        # Wait for any maybe_flush() jobs still in flight for the box we're
        # about to close/finalise, NOT the whole send queue (tried first,
        # reverted: test_streamer_no_backlog_block.py caught it reintroducing
        # a scaled-down version of the exact backlog bug this change fixes,
        # since an unrelated LATER tool-marker's commit_live(blocking=False)
        # would end up waiting behind an EARLIER, still-in-flight maybe_flush
        # job that has nothing to do with it). Two correctness reasons this
        # narrower wait still exists: (1) tg_send_draft reads the CURRENT
        # _draft_id global at execution time, not at enqueue time, if this
        # function goes on to open a NEW box (new _draft_id) before an OLD
        # queued frame for the PREVIOUS box actually runs, that stale frame
        # would land on the NEW draft slot instead. (2) commit_live needs
        # _draft_used_plain (which draft frame format was last actually
        # confirmed) to finalise byte-identically and avoid a flash. Tool-
        # marker/diff-message jobs never touch either, so they're excluded
        # from this counter, see _wait_for_flush_jobs's own docstring.
        _wait_for_flush_jobs()
        # A maybe_flush background job that just finished (waited for
        # above) may have already discovered MESSAGE_TOO_LONG and left it
        # in _pending_recovery for maybe_flush's OWN next call to handle,
        # but if the turn ends right here (message_stop immediately after),
        # there IS no next maybe_flush call, and that signal would
        # otherwise just be lost, silently skipping the split-and-send
        # recovery this whole mechanism exists for. Consume it here too:
        # know going in that the current _buf is already confirmed too
        # long, so the code below can skip straight to the split recovery
        # instead of wasting a real API call re-discovering what we
        # already know.
        known_too_long = _pending_recovery[0] == "too_long"
        if known_too_long:
            _pending_recovery[0] = None
        # commit_live() runs twice per turn in the common case (once at
        # message_stop, again unconditionally when "result" arrives), the
        # second call sees an already-cleared, empty _buf. Guard against
        # that clobbering the real snapshot back to "".
        if _buf:
            _last_committed_text = _buf
        if _live_id is None:
            # Silent-drop salvage. If real text accumulated into _buf but
            # `ensure_live` never managed to open the live box, typically
            # because the placeholder `tg_send("…")` returned None (a tool
            # marker burst blew the per-chat send budget, a transient 5xx,
            # a network blip), every `maybe_flush` early-returned and the
            # buffer would otherwise be lost. This is the "chat dies at
            # end of tool call, full response in claude logs but nothing
            # in Telegram" bug in groups. (DMs don't hit this: ensure_live's
            # draft path sets the live sentinel locally without an API call.)
            # Wait out any active cooldown, then deliver _buf as a fresh
            # message. Skip salvage when _showing_thinking is True, _buf
            # is the "💭 Thinking…" placeholder, not real content, and
            # delivering it would leave a stranded placeholder bubble.
            if _buf and not _showing_thinking:
                if blocking:
                    _wait_for_cooldown("commit_live salvage")
                if tg_send(_buf) is None:
                    if _take_too_long():
                        # Buffer too long for one sendMessage, split and send
                        # each chunk so nothing is silently swallowed.
                        remaining = _buf
                        while remaining:
                            chunk, remaining = _safe_split(remaining, MAX_HTML_CHARS)
                            if chunk and tg_send(chunk) is None:
                                tg_send_plain(chunk)
                    elif blocking and tg_send_plain(_buf) is None:
                        # Both HTML and plain failed (transient outage).
                        # Best-effort plain notification so the user knows to re-ask.
                        tg_api("sendMessage", {
                            "chat_id": TELEGRAM_CHAT_ID,
                            "text": (
                                "⚠️ Response failed to deliver "
                                "(Telegram API error). Please re-ask."
                            ),
                        })
                    elif not blocking:
                        tg_send_plain(_buf)   # one best-effort try, no escalation
            _buf = ""
            _showing_thinking = False
            return
        if _showing_thinking:
            if _live_id == DRAFT_LIVE:
                tg_clear_draft()
                _draft_id = None
            else:
                _delete_placeholder(_live_id)
            _live_id = None
            _buf = ""
            _showing_thinking = False
            last_edit = 0.0
            return
        # End-of-stream delivery must not silently drop. Wait out any active
        # 429 cooldown FIRST so the final attempt actually reaches Telegram
        # instead of being short-circuited by _rate_limited_until. Only when
        # blocking=True, see the docstring above for the mid-turn case.
        if blocking:
            _wait_for_cooldown("commit_live")
        if _live_id == DRAFT_LIVE:
            # Track whether a real message was successfully committed.
            # Empty buffer counts as "committed" (nothing to send, safe to clear).
            committed = not _buf
            if _buf:
                # Finalise in the SAME format the last draft frame used so the
                # real message is byte-identical to what's on screen → no flash.
                # Skip the doomed first attempt entirely if a background
                # maybe_flush job already confirmed this exact buffer is too
                # long (see known_too_long above).
                final_mid = None if known_too_long else (
                    tg_send_plain(_buf) if _draft_used_plain else tg_send(_buf))
                if known_too_long or _take_too_long():
                    head, tail = _safe_split(_buf, MAX_HTML_CHARS)
                    if tail:
                        if head and not _draft_used_plain:
                            tg_send(head)
                        elif head:
                            tg_send_plain(head)
                        tg_send(tail)
                    committed = True  # split path: we delivered something
                elif final_mid is not None:
                    committed = True
                elif blocking:
                    # First attempt failed. Retry once with a fresh wait and
                    # plain-text fallback so the user sees SOMETHING.
                    _wait_for_cooldown("commit_live retry")
                    r1 = tg_send(_buf)
                    if r1 is not None:
                        committed = True
                    elif tg_send_plain(_buf) is not None:
                        committed = True
                    # else: all delivery attempts failed, leave the draft
                    # visible rather than wiping it to show nothing at all.
                # else (mid-turn, not blocking): first attempt failed, drop it,
                # the tool marker and next text still arrive in a fresh box.
            if committed:
                tg_clear_draft()
            # Always clear _draft_id: win or lose, the next turn opens fresh.
            _draft_id = None
        elif _buf:
            # Same known_too_long shortcut as the DRAFT_LIVE branch above.
            ok = False if known_too_long else tg_edit(_live_id, _buf)
            if not ok and (known_too_long or _take_too_long()):
                # Final edit too long for the live message: split, edit live
                # with head (must fit now), send tail as a new message.
                head, tail = _safe_split(_buf, MAX_HTML_CHARS)
                if head:
                    if not tg_edit(_live_id, head):
                        if tg_send(head) is None:
                            tg_send_plain(head)
                if tail:
                    if tg_send(tail) is None:
                        tg_send_plain(tail)
            elif not ok and blocking:
                # Edit failed (cooldown didn't drain, network blip, parse 400).
                # Don't leave the live bubble stuck on "…". Each fallback may
                # trigger a NEW 429 that resets _rate_limited_until; wait it out
                # before each subsequent attempt so we don't silently skip them.
                _wait_for_cooldown("commit_live retry")
                if not tg_edit(_live_id, _buf):
                    _wait_for_cooldown("commit_live send")
                    if tg_send(_buf) is None:
                        _wait_for_cooldown("commit_live plain")
                        if tg_send_plain(_buf) is None:
                            # All delivery attempts failed. Delete the stranded
                            # "…" bubble so the user doesn't see a frozen placeholder.
                            tg_api("deleteMessage", {
                                "chat_id": TELEGRAM_CHAT_ID,
                                "message_id": _live_id,
                            })
                            tg_api("sendMessage", {
                                "chat_id": TELEGRAM_CHAT_ID,
                                "text": "⚠️ Response failed to deliver (Telegram rate limit). Please re-ask.",
                            })
            # else (mid-turn, not blocking, edit failed and not too-long):
            # drop this segment, the tool marker and next text still arrive
            # in a fresh box once ensure_live() reopens with _live_id cleared
            # below, same self-healing tolerance the streaming edit path has.
        _live_id = None
        _buf = ""
        last_edit = 0.0

    def clear_thinking():
        """Drop the "💭 Thinking…" placeholder before real content lands.

        Draft mode: clear the placeholder draft and reset live state so the
        next ensure_live() opens a fresh draft for the actual content. (We
        don't animate from "Thinking with high effort…" → "Sure, let me…",
        which would be a jarring character-by-character delete+retype.)

        Edit mode: keep the same message_id and just blank the buffer; the
        next edit will replace "Thinking…" with the real first chunk in one
        repaint.
        """
        global _showing_thinking, _buf, _live_id, _draft_id
        nonlocal last_edit
        if not _showing_thinking:
            return
        _showing_thinking = False
        _buf = ""
        if _live_id == DRAFT_LIVE:
            tg_clear_draft()
            _draft_id = None
            _live_id = None
            last_edit = 0.0

    def rotate_message(carry):
        """Finalize current live message and start a new one with `carry`."""
        global _live_id, _buf
        nonlocal last_edit
        # blocking=False: this fires mid-stream (buffer overflow while text
        # is still actively being generated), not at turn end. See
        # commit_live's own docstring for why blocking here would let a
        # tool's photo land before the text that should precede it.
        commit_live(blocking=False)       # finalise + reset the overflowing box
        _buf = carry
        if not carry:
            return
        if draft_mode() and not _draft_failed:
            ensure_live()
            maybe_flush(force=True)
        else:
            mid = tg_send(carry)
            if mid is not None:
                _live_id = mid
                last_edit = time.monotonic()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log(f"bad json: {line[:100]}")
                continue

            if not isinstance(ev, dict):
                # /compact and some internal Claude Code events emit bare
                # strings or arrays as NDJSON lines. Skip them, they carry
                # no streaming content we need to forward.
                log(f"skipping non-dict event ({type(ev).__name__}): {str(ev)[:80]}")
                continue

            evtype = ev.get("type")

            if evtype == "stream_event":
                inner = ev.get("event") or {}
                if inner.get("type") == "message_start":
                    _mark_in_turn()
                itype = inner.get("type")

                if itype == "message_start":
                    # New turn: last turn's committed text must not leak
                    # into this one's is_error handling (a genuinely
                    # unannounced error on a turn that streamed nothing
                    # would otherwise wrongly look "already told").
                    global _last_committed_text
                    _last_committed_text = ""
                    # In persistent mode each user message produces its own
                    # claude turn, which means its own message_start. To make
                    # each turn render as its own Telegram message (rather
                    # than overwriting the previous live box), reset the
                    # streaming state here when there's already content.
                    if _buf or _live_id is not None:
                        commit_live()        # finalise prior turn's box (draft→real or edit)
                        finalized = False
                        saw_text = False
                    # Defensive: drop any half-built tool_use entries from a
                    # prior turn. They should already be gone (popped on
                    # content_block_stop), but a truncated stream could leave
                    # a stranded entry that would otherwise leak into the
                    # next turn's block indices.
                    _tool_blocks.clear()
                    _clear_wakeup_if_due()
                    # _emitted_edits is per-turn dedup for Edit/Write/MultiEdit
                    # diff messages. In persistent mode the streamer process
                    # serves many turns; without this clear, the set would
                    # grow unbounded AND a future turn that legitimately
                    # repeats a tool_use id (the SDK occasionally re-emits
                    # assistant envelopes on retried tool_results) would
                    # silently skip the diff. Reset per turn.
                    _emitted_edits.clear()
                    # Drop any pending result-size annotations from the prior
                    # turn. A tool whose result never arrived (e.g. the spawn
                    # crashed mid-turn) should not leak into the next turn.
                    _tool_call_mids.clear()
                    # _draft_failed is a per-TURN signal that this turn's
                    # draft API call failed and we fell back to the edit
                    # path. Without resetting per turn, a single transient
                    # 5xx draft failure stickily forces every subsequent
                    # turn into the edit path for the rest of the spawn,
                    # the user loses the smooth DM streaming animation
                    # until the next /cancel.
                    global _draft_failed, _draft_used_plain, _parse_entity_errs
                    _draft_failed = False
                    _parse_entity_errs = 0
                    # _draft_used_plain is set by tg_send_draft when a frame
                    # succeeds; it's per-box state, not per-turn. Reset
                    # defensively at turn boundary so a previous turn's
                    # plain-fallback can't make commit_live() finalise THIS
                    # turn's HTML draft as plain text and cause a flash.
                    _draft_used_plain = False
                    # Refresh verbosity once per turn. Changes the user made
                    # via the menu between turns land here; changes mid-turn
                    # don't half-apply because the rest of this turn keeps
                    # reading the snapshot.
                    _verbosity = _load_verbosity()
                    # Mirror Claude Code's "Thinking…" indicator: prime the
                    # live box with a placeholder that streams in like any
                    # other reply. clear_thinking() drops it the instant a
                    # real text_delta or tool marker arrives, and commit_live
                    # tears it down on tool-only / empty turns, so the
                    # original orphan-placeholder bug doesn't reappear.
                    # Suppressed entirely when verbosity.thinking == "off".
                    if _verbosity.get("thinking", "on") == "on":
                        _showing_thinking = True
                        effort = _read_effort()
                        _buf = (f"💭 Thinking with {effort} effort…"
                                if effort else "💭 Thinking…")
                        ensure_live()
                        maybe_flush(force=True)

                elif itype == "content_block_start":
                    cb = inner.get("content_block") or {}
                    cb_type = cb.get("type")
                    idx = inner.get("index")
                    if cb_type == "text" and _buf and not _buf.endswith("\n\n"):
                        # Claude emits each chunk of narration as a separate
                        # text content_block (pre-tool reasoning, post-tool
                        # commentary, final answer). Without a separator,
                        # they concatenate into one wall of run-on prose like
                        # "...as required.Good, config loaded.Now I'll..."
                        # Insert a paragraph break so each block is its own
                        # paragraph in the live message.
                        _buf += "\n" if _buf.endswith("\n") else "\n\n"
                    elif cb_type == "tool_use":
                        # Register the in-progress tool_use; the marker is
                        # emitted on content_block_stop once `input_json` has
                        # fully streamed in via input_json_delta events. We
                        # used to emit a name-only marker here ("⚙️ *Bash*"),
                        # which didn't say WHICH bash/WHICH file, turning
                        # the chat into an opaque "tool icon soup".
                        # Waiting for the input typically adds <1s and lets
                        # us show "⚙️ *Bash* `ls -la /tmp`" instead.
                        #
                        # We key by content_block `index`. Falls back to None
                        # when an event omits index (older synthetic streams,
                        # smoke tests), start/stop still match because the
                        # block must close before the next opens.
                        name = cb.get("name", "")
                        if name:
                            _tool_blocks[idx] = {
                                "name": name, "input_json": "",
                                "id": cb.get("id", "")}
                    elif cb_type == "thinking":
                        # Its text (if any) is narration, see thinking_delta
                        # below. Most thinking blocks stay empty, so the
                        # paragraph separator is added lazily on the first
                        # visible chunk rather than here.
                        thinking_sep_pending = True

                elif itype == "content_block_delta":
                    delta = inner.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype in ("text_delta", "thinking_delta"):
                        # Some models send the short notes written between
                        # tool calls as thinking blocks, not text blocks.
                        # Real reasoning arrives with empty text (it's never
                        # exposed), so any non-empty thinking text is that
                        # narration and renders exactly like text. Dropping
                        # it hid every between-tool note.
                        if dtype == "text_delta":
                            chunk = delta.get("text", "")
                        else:
                            chunk = delta.get("thinking", "")
                        if not chunk:
                            continue
                        clear_thinking()
                        _compact_close()
                        if dtype == "thinking_delta" and thinking_sep_pending:
                            thinking_sep_pending = False
                            if _buf and not _buf.endswith("\n\n"):
                                _buf += "\n" if _buf.endswith("\n") else "\n\n"
                        saw_text = True
                        ensure_live()
                        new_buf = _buf + chunk
                        # Rotate when EITHER the markdown soft cap is hit OR
                        # the HTML-rendered length approaches Telegram's 4096
                        # ceiling. The HTML check is the real one, markdown
                        # like **bold** / `code` / pipe-tables expands a lot,
                        # so a 3800-markdown-char buffer can render to >4096
                        # html chars and trip MESSAGE_TOO_LONG forever (until
                        # rotation). We only pay the html_format cost when
                        # the markdown is large enough to plausibly cross
                        # the line, under 2000 chars no realistic expansion
                        # exceeds the limit.
                        too_long = (
                            len(new_buf) > MAX_MSG_CHARS or
                            (len(new_buf) > 2000
                             and len(html_format(new_buf)) > MAX_HTML_CHARS)
                        )
                        if too_long:
                            rotate_message(chunk)
                        else:
                            _buf = new_buf
                            maybe_flush()
                        # Scan the growing buffer once we have ≥40 chars,
                        # enough for "Failed to authenticate" or
                        # "401 Invalid authentication" to be in flight.
                        if len(_buf) >= 40:
                            check_auth_failure(_buf)
                    elif dtype == "input_json_delta":
                        # Streamed JSON for a tool_use input. Accumulate until
                        # content_block_stop, then parse + render with preview.
                        idx = inner.get("index")
                        if idx in _tool_blocks:
                            _tool_blocks[idx]["input_json"] += delta.get(
                                "partial_json", "") or ""

                elif itype == "content_block_stop":
                    idx = inner.get("index")
                    tb = _tool_blocks.pop(idx, None)
                    if tb:
                        # Tool_use block finalised. Parse the accumulated
                        # input JSON.
                        try:
                            inp = (json.loads(tb["input_json"])
                                   if tb["input_json"] else {})
                        except Exception:
                            inp = {}
                        if tb["name"] == "ScheduleWakeup" and tb.get("id"):
                            _wakeup_inputs[tb["id"]] = inp
                        # Verbosity gate. Edit-family tools (Edit/Write/
                        # MultiEdit) are governed by verbosity.edits; any
                        # other tool by verbosity.tool_calls. When edits ==
                        # "diff" this bare marker is ALSO suppressed (not
                        # just "off"): the assistant-envelope handler below
                        # sends a fuller message for the same tool_use_id
                        # that already carries this exact "📝 *Edit*
                        # `path`" header (see _format_edit_tool) plus the
                        # diff body. Sending both was a double-message bug,
                        # every edit landed as two separate Telegram
                        # messages, the bare marker immediately followed by
                        # the diff.
                        is_edit = tb["name"] in EDIT_TOOL_NAMES
                        compact = (
                            (is_edit and _verbosity.get("edits") == "compact")
                            or (not is_edit
                                and _verbosity.get("tool_calls") == "compact")
                        )
                        if compact:
                            commit_live(blocking=False)
                            if tb["name"] == "ask_user":
                                _compact_close()   # its question comes next
                            else:
                                _compact_add(_compact_item(tb["name"], inp))
                            continue
                        if is_edit and _verbosity.get("edits") == "diff":
                            continue
                        _compact_close()
                        line = _tool_line(tb["name"], inp)
                        # Every tool marker is its own Telegram message.
                        # commit_live() handles the "💭 Thinking…" placeholder
                        # teardown via its _showing_thinking branch (which
                        # deletes the placeholder in edit/group mode and
                        # clears the draft in DM mode). Calling clear_thinking
                        # first would set _showing_thinking=False and skip
                        # that teardown, leaving the placeholder orphaned in
                        # group chats, the "thinking persists / turn cuts
                        # off after tool call" bug. commit_live also commits
                        # any pre-tool text so prose-then-tool renders as
                        # TWO messages, not bundled. The standalone tg_send
                        # below doesn't track a new live id, so the NEXT
                        # text_delta opens a fresh box, giving us tool/text/
                        # tool/text as discrete bubbles instead of one wall.
                        #
                        # This call fires mid-turn, the tool itself
                        # (e.g. a Bash call to notify-cmd/tg.sh sending a
                        # photo) executes independently of the streamer and
                        # isn't gated on it, so a blocking cooldown wait here
                        # (up to 90s, and logs show multi-second waits are
                        # common) let the photo land long before this
                        # pre-tool text and the tool marker below did.
                        # blocking=False: see commit_live's own docstring.
                        commit_live(blocking=False)
                        # ask_user sends its own question bubble with buttons via
                        # the MCP server; the tool marker here is redundant and
                        # races to arrive AFTER the buttons (different send paths).
                        if tb["name"] == "ask_user":
                            continue
                        # Previously a test photo could land, then its OWN
                        # tool marker take 6 more seconds (zero 429s in that
                        # window). This send must not wait behind, or add
                        # to, the same backlog maybe_flush's calls were
                        # causing; see the _send_queue comment near the top
                        # of this file.
                        tool_id = tb.get("id", "")
                        def _job(line=line, tool_id=tool_id, is_edit=is_edit):
                            mid = tg_send(line)
                            if mid and tool_id and not is_edit:
                                _tool_call_mids[tool_id] = {"mid": mid, "text": line}
                        _bg_marker(_job)

                elif itype == "message_stop":
                    commit_live()
                    finalized = True

            elif evtype == "assistant":
                # Top-level assistant envelope. If we haven't streamed any
                # text yet (e.g. partial-messages off), use this as fallback.
                msg = ev.get("message") or {}

                if not saw_text:
                    for cb in (msg.get("content") or []):
                        if cb.get("type") == "text":
                            t = cb.get("text", "")
                            if t:
                                _buf += t
                                saw_text = True
                    if saw_text:
                        ensure_live()
                        maybe_flush(force=True)
                # Auth-failure scan over every text block on the envelope
                # (covers the case where deltas come in too small to trip
                # the inline check, e.g. partial-messages off).
                for cb in (msg.get("content") or []):
                    if cb.get("type") == "text":
                        check_auth_failure(cb.get("text", "") or "")
                # Render Edit/Write/MultiEdit tool calls as their own
                # Telegram messages with a code block. The streaming
                # path doesn't have the full tool input until the
                # assistant envelope arrives, so this is the right hook.
                # Gated by verbosity.edits, only "diff" sends the
                # separate message; "marker" leaves the inline 📝 line
                # alone in the live buffer; "off" already short-circuited
                # the marker emission above.
                if _verbosity.get("edits") == "diff":
                    for cb in (msg.get("content") or []):
                        if cb.get("type") != "tool_use":
                            continue
                        name = cb.get("name", "")
                        if name not in EDIT_TOOL_NAMES:
                            continue
                        tool_use_id = cb.get("id", "")
                        if tool_use_id in _emitted_edits:
                            continue
                        _emitted_edits.add(tool_use_id)
                        inp_data = cb.get("input") or {}
                        diff_msg = _format_edit_tool(name, inp_data)
                        if diff_msg:
                            _compact_close()
                            # Finalise the current live box first so the
                            # diff arrives as its own clean message below.
                            # blocking=False: mid-turn, same reasoning as the
                            # tool-marker call site above. The actual send
                            # is backgrounded too, same as the tool marker.
                            commit_live(blocking=False)
                            _bg_marker(lambda msg=diff_msg: tg_send(msg))

            elif evtype == "user":
                # Tool results arrive in the user turn that follows the
                # assistant's tool_use blocks. For each tool_result we look
                # up the Telegram message we sent for that tool call and edit
                # it to append the output size, mirroring Claude Code CLI's
                # "(+N lines)" annotation so the user can see how much the
                # tool returned without the full text being in the chat.
                msg = ev.get("message") or {}
                content = msg.get("content")
                if ev.get("isReplay"):
                    # An inbox line Claude just read (see _note_replay).
                    _note_replay(content)
                    continue
                # Not always a block list: after a /compact, Claude Code sends
                # the conversation summary (and "Compacted") as a user message
                # whose content is a plain string. Iterating that walked it
                # character by character and crashed the whole streamer on
                # str.get(), so the compact confirmation never sent.
                if not isinstance(content, list):
                    continue
                for cb in content:
                    if not isinstance(cb, dict) or cb.get("type") != "tool_result":
                        continue
                    tid = cb.get("tool_use_id", "")
                    result_content = cb.get("content") or []
                    if isinstance(result_content, list):
                        text_out = "\n".join(
                            c.get("text", "") for c in result_content
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
                    else:
                        text_out = str(result_content or "")
                    if tid in _wakeup_inputs:
                        _note_wakeup_result(_wakeup_inputs.pop(tid), text_out)
                    tracked = _tool_call_mids.pop(tid, None)
                    if not tracked:
                        continue
                    n = len(text_out.splitlines())
                    if n >= 20:
                        tg_edit(tracked["mid"], tracked["text"] + f"\n_+{n} lines_")

            elif evtype == "result":
                # Final result envelope. Finalise whatever we have.
                commit_live()
                _compact_close()
                finalized = True
                # Before the footer: clear any "💭 Thinking…" bubble whose
                # delete was skipped by a 429 cooldown earlier in the turn.
                _retry_stale_placeholders()
                metrics.emit("turn_end", room=str(TELEGRAM_CHAT_ID),
                             duration_ms=ev.get("duration_ms"),
                             is_error=bool(ev.get("is_error")),
                             ctx_pct=ctx_pct_from_result(ev),
                             version=os.environ.get("BOT_CODE_VERSION", "unknown"))
                # Bump the per-room completed-turns counter. spawn_claude.sh's
                # crash/rate-limit salvage uses it to re-queue ONLY inbox
                # messages whose turn never completed (the inbox keeps every
                # line of the spawn's lifetime, so without this a crash after
                # N good turns re-delivered all N answered messages too).
                # Error results don't count: their message deserves a retry.
                # _this_turn_was_compact is read later OUTSIDE this branch
                # (the duration-footer suppression below), so it must be
                # defined regardless of which side of is_error this turn
                # took, not just inside the not-is_error path.
                _this_turn_was_compact = False
                _is_interrupt = (ev.get("subtype") == "error_during_execution"
                                 and _room_path(".interrupt_requested").exists())
                _answered = _close_turn(ev)
                _record_ctx(ev)
                if not ev.get("is_error") or _is_interrupt:
                    # Every inbox line this turn read is now answered. No
                    # reaction for that: 👀 (set when Claude read the line)
                    # stays. A reaction at the end of a long reply notified
                    # the user about a message 30 bubbles up, which they had
                    # to scroll back to just to clear.
                    _write_read_count()
                # Any other error (rate limit, a too-full context) leaves
                # .turns_done short of these lines, so the crash salvage
                # re-queues them; the prompt-too-long recovery below
                # re-sends them itself.
                if not ev.get("is_error"):
                    # Compact-in-flight: if a /compact was injected via the
                    # menu button, fire a notify-cmd confirmation so the user
                    # gets an explicit alert that compression completed.
                    try:
                        _cif = (REPO / "state" / "rooms"
                                / str(TELEGRAM_CHAT_ID) / ".compact_in_flight")
                        if _cif.exists():
                            _cif.unlink()
                            _this_turn_was_compact = True
                            # Skipped when the progress bubble (_on_system)
                            # already said "Context compressed".
                            if not _compact_announced:
                                import subprocess as _sp
                                _nc = bot_config.notify_cmd()
                                _sp.run([_nc, "--tag", "[compact]",
                                         "Context compressed."],
                                        capture_output=True, timeout=5)
                    except Exception:
                        pass
                if _parse_entity_errs > 0:
                    log(f"parse-entity errors this turn: {_parse_entity_errs} "
                        f"(sendMessageDraft 400, fell back to edit path)")
                    _parse_entity_errs = 0
                # If we somehow streamed nothing but result has text, send it.
                # Gated on NOT is_error: for an error result the branch below
                # already sends the right user-facing message (a formatted
                # rate-limit notice, an auth alert, or a generic error line),
                # so this raw dump was pure redundant noise on top of that,
                # not new information. Before this fix it happened for both
                # rate-limit results ("Claude AI usage limit reached|<epoch>"
                # landing as its own message right before the proper "⏸ Rate
                # limited..." one) and structured auth-failure results (see
                # the is_error branch's own comment below).
                if _live_id is None and not saw_text and not ev.get("is_error"):
                    final = ev.get("result") or ""
                    if final:
                        tg_send(final)
                # If claude reported an error, surface it.
                _err_text = str(ev.get("result") or "")
                if _is_interrupt:
                    # The 🛑 cancel button interrupted this turn (see
                    # commands/cancel.sh); Claude moves on to the next inbox
                    # line by itself, so there is no error to report.
                    _room_path(".interrupt_requested").unlink(missing_ok=True)
                    tg_send("🛑 _Interrupted._")
                elif (ev.get("is_error")
                        and any(k in _err_text.lower() for k in PTL_MARKERS)):
                    # The context was too full to take the message. Claude
                    # Code does not compact first in this case, so queue a
                    # /compact and re-send the message after it.
                    if _recover_prompt_too_long(_answered):
                        tg_send("🗜 _Context is full. Compressing it now, then I'll answer your message._")
                    else:
                        tg_send("⚠️ That message doesn't fit in the context even after compressing. Try a shorter one, or start a new session from the menu.")
                elif ev.get("is_error"):
                    # The result text first: "subtype" is "success" even on
                    # many error results, which used to surface as the
                    # meaningless "⚠️ claude error: success".
                    _sub = ev.get("subtype")
                    raw_err = str(ev.get("api_error_status") or _err_text
                                  or (_sub if _sub != "success" else "")
                                  or "error")
                    # Check the FULL result text for the auth-failure phrases
                    # BEFORE the generic-fallback decision below, not just
                    # raw_err's narrow priority order (api_error_status,
                    # e.g. bare "401", first). A structured is_error result
                    # that carries a real auth phrase only in `result` used
                    # to slip past this narrower check, fall through to the
                    # generic "⚠️ claude error: 401" message, AND THEN also
                    # trip the proper auth alert from the later full-text
                    # scan below, three separate messages for one failure.
                    # check_auth_failure's one-shot guard makes calling it
                    # twice with matching text safe (the second call is a
                    # no-op once auth_alert_sent is set).
                    check_auth_failure(str(ev.get("result") or "").lower())
                    check_auth_failure(raw_err.lower())
                    # claude's own rate-limit handling sometimes streams a
                    # native banner ("You've hit your session limit ·
                    # resets ...") as ordinary text before the result even
                    # closes out is_error, that already told the user
                    # everything our own formatted message would. Sending
                    # both back to back (two separate assistant messages for
                    # one hit) is pure duplication,
                    # so skip ours whenever claude already said it.
                    already_told = (
                        any(kw in raw_err.lower() for kw in _RATE_LIMIT_KEYWORDS)
                        and any(kw in _last_committed_text.lower() for kw in _RATE_LIMIT_KEYWORDS)
                    )
                    is_rate_err = any(kw in raw_err.lower() for kw in _RATE_LIMIT_KEYWORDS)
                    if (not auth_alert_sent and not already_told
                            and not (is_rate_err and _rate_notice_recent())):
                        tg_send(_fmt_error_result(raw_err))
                        if is_rate_err:
                            _mark_rate_notice()
                    # If this is a rate-limit error, park the room and exit.
                    # In persistent mode claude stays alive after a 429 result
                    # and the next inbox line hits the same limit again. Exiting
                    # here propagates SIGPIPE back through tee into claude so the
                    # spawn_claude.sh exit handler fires and runs salvage + alerts.
                    if any(kw in raw_err.lower() for kw in _RATE_LIMIT_KEYWORDS):
                        _park_room_for_rate_limit()
                        sys.exit(0)
                # Also scan the final result text, same one-shot alert.
                # Covers the DOCUMENTED real shape (auth failure as plain
                # streamed text, is_error False) that the branch above never
                # sees at all.
                check_auth_failure(str(ev.get("result") or ""))
                # Turn-duration + context-level footer. Sent as its own small
                # message so it never disturbs the streamed reply or trips the
                # DM no-flash rule. Skipped when duration is absent (synthetic
                # streams / smoke tests) so nothing spurious sends.
                #
                # Also skipped (send only, not the _prev_ctx_pct bookkeeping)
                # for the compact turn itself: a manual /compact's own result
                # carries a real duration_ms, so without this it fired a
                # SECOND, redundant confirmation right after "Context
                # compressed.", this one phrased "⏱ Worked for Xs ✂️
                # compacted 100%→7%", which reads as the assistant still doing
                # something rather than having just finished (confusing
                # after tapping 🗜 compact at a natural stopping point). _prev_ctx_pct still updates
                # so the NEXT real turn's footer compares against the
                # post-compaction baseline instead of re-detecting the same
                # drop a turn late.
                global _prev_ctx_pct
                _dms = ev.get("duration_ms")
                _dstr = fmt_duration(_dms) if _dms is not None else ""
                if _dstr:
                    ctx_pct = ctx_pct_from_result(ev)
                    ctx_part = ""
                    compact_note = ""
                    if ctx_pct is not None:
                        if (_prev_ctx_pct is not None
                                and _prev_ctx_pct - ctx_pct > 25):
                            # Large drop in context = auto-compact fired.
                            compact_note = (
                                f" ✂️ compacted "
                                f"{_prev_ctx_pct:.0f}%→{ctx_pct:.0f}%"
                            )
                            ctx_part = compact_note
                        else:
                            warn = " ⚠️" if ctx_pct >= 75 else ""
                            ctx_part = f" · 📊 {ctx_pct:.0f}%{warn}"
                        _prev_ctx_pct = ctx_pct
                    if not _this_turn_was_compact:
                        rl_part = _rate_limit_footer()
                        # Every turn-end footer gets a Menu shortcut so the
                        # user isn't stuck typing to reach it; a Compact
                        # button joins it specifically when context is in
                        # the same ⚠️ warning band as ctx_part's own emoji
                        # above, so the one moment compacting is actually
                        # worth doing is exactly when the button appears.
                        #
                        # The ordinary act:menu / act:compact verbs EDIT the
                        # tapped message in place, which is exactly right
                        # for the main menu's own navigation but wrong
                        # here, it overwrote this footer's own duration/
                        # context text. act:menu_new / act:compact_new
                        # (commands/menu_new.sh, commands/compact_new.sh)
                        # are footer-only wrappers that force a fresh
                        # message instead, leaving the footer untouched.
                        footer_kb = [[{"text": "☰ Menu", "callback_data": "act:menu_new"}]]
                        if ctx_pct is not None and ctx_pct >= 75:
                            footer_kb[0].append(
                                {"text": "🗜 Compact", "callback_data": "act:compact_new"})
                        # This is a ONE-SHOT tg_send,
                        # not a streaming edit frame. tg_api's docstring
                        # is explicit that a dropped frame during a 429
                        # cooldown is fine for the edit-in-place path
                        # ("the next successful flush catches up"), but
                        # there IS no next flush for the footer, if this
                        # exact call lands inside an active cooldown
                        # window it silently vanishes forever. Logs show
                        # 429s every few minutes under normal load, so
                        # this isn't rare.
                        # commit_live() already solved the identical
                        # problem for the streamed reply's own final
                        # delivery via _wait_for_cooldown ("the streamer
                        # is at end-of-stream by then, so a short sleep
                        # doesn't matter"); the footer fires at the exact
                        # same point in the turn's lifecycle, so the same
                        # justification applies. Deliberately NOT applied
                        # to tg_send generally, tool markers fire MID-turn
                        # while claude may still be producing output, and
                        # blocking there is the exact wedging bug tg_api's
                        # own docstring already warns about.
                        #
                        # Retry once on failure, same shape as commit_live's
                        # own retries: the FIRST attempt can itself trigger a
                        # fresh 429 (resetting _rate_limited_until to a new,
                        # later deadline), so the retry needs its own
                        # _wait_for_cooldown call, not just the one before
                        # the first attempt.
                        footer_text = f"⏱ _Worked for {_dstr}{ctx_part}{rl_part}_"
                        _wait_for_cooldown("turn-end footer")
                        if tg_send(footer_text, keyboard=footer_kb) is None:
                            _wait_for_cooldown("turn-end footer retry")
                            tg_send(footer_text, keyboard=footer_kb)

                # Last, after this reply's footer: it marks the NEXT result
                # as the compaction.
                _compact_now_if_full(ev)

            elif evtype == "rate_limit_event":
                # Cache the latest snapshot per limit type. /usage reads this
                # as a fallback when the scrape is stale. Anthropic only fills
                # utilization when status is allowed_warning or blocked; carry
                # the previous-cycle value forward unless the reset window changed.
                info = ev.get("rate_limit_info") or {}
                rtype = info.get("rateLimitType")
                if rtype:
                    try:
                        try:
                            cache = json.loads(USAGE_CACHE.read_text())
                        except Exception:
                            cache = {}
                        prev = cache.get(rtype) or {}
                        new_util = info.get("utilization")
                        new_resets = info.get("resetsAt")
                        if (new_util is None and new_resets
                                and prev.get("resetsAt") == new_resets):
                            new_util = prev.get("utilization")
                        cache[rtype] = {
                            "utilization": new_util,
                            "status": info.get("status"),
                            "resetsAt": new_resets,
                            "isUsingOverage": info.get("isUsingOverage"),
                            "updatedAt": int(time.time()),
                        }
                        USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
                        USAGE_CACHE.write_text(json.dumps(cache, indent=2))
                    except Exception:
                        pass

            elif evtype == "system":
                _on_system(ev)

            elif evtype == "control_response":
                # An interrupt that arrived between turns ends nothing, so
                # no result will clear its marker; clear it here instead so
                # the next 🛑 tap is not mistaken for a stuck interrupt.
                if not _in_turn:
                    _room_path(".interrupt_requested").unlink(missing_ok=True)

            # unknown: skip

    except Exception as e:
        log(f"main loop crashed: {e}")
        # Drain both background senders FIRST: a crash here can happen right
        # after enqueueing a send (e.g. a tool marker) that hasn't actually
        # gone out yet. Without this, the process below still runs its own
        # crash-recovery sends correctly, but any already-queued background
        # job would be silently lost the moment this process exits (daemon
        # threads don't get a chance to finish). See the module-level
        # docstring near the top of this file for why there are two queues.
        _edit_queue.join()
        _marker_queue.join()
        if _showing_thinking and _live_id is not None:
            # Placeholder-only state: drop the placeholder rather than
            # appending "(stream broken)" to "Thinking with X effort...",
            # which reads as nonsense to the user. They see nothing
            # instead of garbage, and the spawn wrapper will surface the
            # real exit reason via logs/spawn.log if needed.
            if _live_id == DRAFT_LIVE:
                tg_clear_draft()
            else:
                tg_api("deleteMessage", {
                    "chat_id": TELEGRAM_CHAT_ID, "message_id": _live_id,
                })
        elif _live_id is not None and _buf:
            if _live_id == DRAFT_LIVE:
                tg_clear_draft()
                tg_send(_buf + "\n\n(stream broken)")
            else:
                tg_edit(_live_id, _buf + "\n\n(stream broken)")
        sys.exit(1)

    # End of stdin: finalise whatever is live.
    if not finalized:
        commit_live()
    if _live_id is None and not saw_text:
        tg_send("(claude returned no output)")
    # Wait for every backgrounded send (maybe_flush's edits, tool markers,
    # diff messages, see the module-level docstring near the top of this
    # file for why there are two separate queues) to actually go out before
    # this process exits. Without this, anything still queued when stdin
    # closes would be silently dropped, background threads are daemon
    # threads, they don't get to finish once the main thread ends. Same
    # "blocking here is fine, this really is the end" reasoning as
    # _wait_for_cooldown's own doc comment.
    _edit_queue.join()
    _marker_queue.join()


if __name__ == "__main__":
    main()
