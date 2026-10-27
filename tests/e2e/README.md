# tests/e2e/

Full end-to-end tests: the REAL `listener.sh` -> `spawn_claude.sh` ->
`stream_to_telegram.py` pipeline, run as real subprocesses against two fakes
instead of the real Telegram Bot API and the real `claude` CLI:

- `fakes/fake_telegram_server.py` - a real local HTTP server standing in for
  `api.telegram.org`. Records every call, lets a test inject incoming
  "messages"/button taps and configure `getChatMember`/error responses.
- `fakes/fake_claude.py` - stands in for the `claude` binary. Reads a small
  JSON script fixture (per-room turn lists) and emits real-shaped stream-json
  events per turn: normal replies, rate limits, crashes, auth failures, and
  genuinely-hanging tool calls.

Why this exists: `tests/run.sh` mocks Telegram at the HTTP layer inside a
single Python process (see `tests/test_group_thinking_teardown.py` and
friends) to unit-test `stream_to_telegram.py` in isolation. That's fast and
good for rendering logic, but it can't catch bugs in how the *processes*
interact: queueing, the rate-limit park/resume cycle, crash salvage, the
circuit breaker, the listener's dynamic poll cadence, multi-room isolation.
This harness runs the real scripts as real subprocesses so those
interactions are exercised for real, without touching production Telegram
or spending real Claude usage. Every seam it needs (`TELEGRAM_API_BASE`,
`CLAUDE_BIN_OVERRIDE`) defaults to real production behaviour when unset.

## Running

```
bash tests/e2e/run.sh                  # every scenario
python3 tests/e2e/test_basic_roundtrip.py       # one scenario directly
KEEP_SANDBOX=1 python3 tests/e2e/test_X.py      # keep the temp sandbox on
                                                   exit for postmortem (path
                                                   printed to stderr; also
                                                   kept automatically on any
                                                   test failure)
```

Each scenario takes a handful of real seconds to tens of seconds (subprocess
startup, Telegram edit-throttle intervals, and for the crash-loop/circuit-
breaker scenario, a real compounding retry sequence), so this suite is
separate from `tests/run.sh` and not part of the fast inner dev loop. The
full suite currently takes roughly 5-8 minutes.

## Scenario catalog

| File | What it exercises |
|---|---|
| `test_basic_roundtrip.py` | The harness's own smoke test: DM in, reply out. If this fails, nothing else here can be trusted. |
| `test_rate_limit_recovery.py` | A 429 kill parks the room, salvages + queues the message, and auto-resumes with full context once the window clears. |
| `test_crash_vs_rate_limit_session.py` | Contrasts the two exit paths directly: both a rate limit AND a single (non-looping) crash PRESERVE session_id; only a real crash loop costs it (see `test_circuit_breaker.py`). Also confirms a crash is never painted as a rate limit. |
| `test_circuit_breaker.py` | A single poisoned message that keeps crashing compounds via real salvage-and-retry into >=3 crashes within 60s, trips the breaker, truncates inbox+queue, resets session_id, and the loop genuinely stops (not just "sends one more message about it"). |
| `test_stale_user_cancel_marker.py` | Regression test: a leaked `.user_cancel` marker (from an earlier `/cancel`/`/new`/`/cd` whose own exit went through the TERM-trap `cleanup()` path, which never deleted it) used to survive and misclassify a LATER, unrelated crash as "user cancelled", silently skipping crash detection and the salvage/retry that depends on it. Confirms a real cancel's marker never leaks past cleanup(), and that an artificially-leaked stale marker is ignored (age-gated) rather than hijacking a genuine later crash. |
| `test_cancel_mid_turn.py` | `/cancel` against a turn that's genuinely in flight (a hanging tool call, not one that happened to already finish), via `act:cancel yes`. Confirms the spawn process tree actually dies, state is wiped, and the room isn't wedged afterward. |
| `test_cancel_races_exit.py` | `/cancel` fired at the exact moment a spawn is ALSO naturally exiting on its own (a rate limit or a crash), using fake_claude.py's `"delay"` field to create a real timing window rather than guessing with sleeps. Since kill_room_spawn writes `.user_cancel` BEFORE sending the signal, the outcome must be identical regardless of which side wins: session_id preserved, marker never leaks, no stray `.crash_times`/`.rate_limited_until` left behind, room answers normally afterward. Also covers a rapid double-`/cancel` tap as a safe no-op. |
| `test_no_admin_warning.py` | The one-time "bot is not admin" warning: fires once for a non-admin group, never for an admin group, never for a DM, never twice. |
| `test_user_approval_gating.py` | The per-room allowlist: an unapproved sender is silently dropped and captured to pending_users.json (never reaches claude); an approved one is dispatched normally; the owner is always allowed everywhere without ever being written to approved_users.json. |
| `test_group_mention_gating.py` | The `mentions_only` toggle: an un-mentioned group message is ignored entirely; a mentioned one is dispatched with the `@mention` stripped before reaching claude. |
| `test_multi_user_group_conversation.py` | Two different real people sharing one group room: each message is prefixed with the CORRECT sender's name (not just "a" name), and the same people DMing separately stay unprefixed. |
| `test_multi_room_isolation.py` | Three rooms (a DM and two groups) firing interleaved, concurrent, real persistent claude processes, each with its own scripted conversation. Asserts zero cross-room contamination. Also the regression test for the harness's own per-room turn-tracking design. |
| `test_long_reply_splitting.py` | A reply well over Telegram's ~4096-char cap splits into multiple messages, each individually under the cap, with no content lost at a split seam. |
| `test_auth_failure_vs_rate_limit.py` | An expired/invalid OAuth token is a DIFFERENT failure mode from a rate limit: asserts BOTH the documented plain-text auth-failure shape AND a structured `is_error` shape each produce exactly one clean alert (fixed from three redundant messages, see Known findings below), and that neither is misclassified as a rate limit. |
| `test_rate_limit_auto_resume_no_nudge.py` | The direct test that a parked room resumes unprompted: after a rate-limit park, sends ZERO further activity (no nudge message, nothing) and confirms the room still auto-resumes on its own within the listener's natural idle-poll cadence. |
| `test_rate_limit_survives_listener_restart.py` | A listener process restart (♻️ menu / launchd kickstart) WHILE a room is parked on a rate-limit hold must not lose or corrupt the hold, and must not retry prematurely right after boot. |
| `test_multi_room_restart_survival.py` | The harder multi-room version: three rooms in three DIFFERENT states (one genuinely mid-turn on a hanging tool call, one rate-limited, one untouched) all survive ONE restart independently. Verifies the CLAUDE.md claim "detached spawns in other rooms survive" a restart end to end for the first time: the in-flight spawn keeps the SAME pid (not killed and respawned), a REAL `/cancel` dispatched through the NEW listener process can still kill a spawn the OLD process launched (proves `kill_room_spawn` truly only depends on the pid recorded in `busy.lock` on disk, nothing in-memory), the rate-limited room's hold and no-nudge auto-resume both survive, and the untouched room dispatches normally on the fresh process. |
| `test_message_while_rate_limited.py` | A message sent WHILE already rate-limited gets a 😴 reaction and queues, instead of spawning a doomed retry into the same limit; confirms it's still answered once the window clears. This is the test that caught the character-eating queue-coalescing bug, see Known findings below. |
| `test_multi_user_failure_combos.py` | Rate limit and auth failure combined with MULTIPLE real people in one group room: a failure is room-scoped, not per-sender, so a different person's message during a hold gets the same 😴-react-and-queue treatment a same-person follow-up would, both people's messages survive coalescing with names intact, and an auth alert fires exactly once total regardless of how many different people message the room afterward, not once per sender. |
| `test_compact_flow.py` | All three code paths in commands/compact.sh: live injection (busy, inbox ready: `/compact` goes straight into the inbox, `.compact_in_flight` clears once "Context compressed." fires, and a real follow-up message still gets answered afterward, the actual "nothing breaks" check), pending-while-busy-not-ready (`.compact_pending`, "Queued context compression."), and pending-while-idle (same marker, resolved on the room's next spawn, injected BEFORE the real message that triggered it). Also two real regressions this coverage found: (1) a manual compact's own result carries `duration_ms` like any real turn, which used to trigger a SECOND, redundant "⏱ Worked for Xs ✂️ compacted 100%→7%" footer right after "Context compressed."; fixing it required teaching `fake_claude.py` to emit `duration_ms`/`ctx_pct` on a turn at all (see its docstring), since that whole footer path had never once been exercised against the fake. (2) The more serious one, found only after the first fix turned out not to be enough: a live-injected `/compact` still unconsumed when the spawn crashes used to get salvaged into `queue.jsonl` as plain text, where the next spawn's queue-coalescing merged it with real pending content into ONE blob, so claude just answered the literal string "/compact" as ordinary conversational text (real tool calls, real replies) instead of it ever triggering silent compaction. Fixed by re-arming `.compact_pending` in the salvage path instead of queuing raw text. |
| `test_stale_spawn_refresh.py` | dispatch()'s stale-spawn refresh: a persistent spawn reads its script text once at exec time, so it stays oblivious to any later code edit until it happens to crash/rate-limit/get `/cancel`'d, which can mean a long stretch of running stale code past several shipped fixes. dispatch() now compares a spawn's `.spawn_started_at` against the newest mtime across scripts/+hooks/ (`code_mtime()` in lib.sh) on every message, and transparently kills-and-refreshes a stale one via `kill_room_spawn(rd, "stale")` before routing the message. Asserts the refresh is genuinely invisible (no "cancelled" badge, session_id preserved, `.crash_times` untouched, not a real crash) and that an up-to-date spawn is left alone (same pid keeps answering, the ordinary case every other test in this suite already exercises without incident). |
| `test_menu_buttons_e2e.py` | Menu-driven actions (`act:menu`, `act:effort`, `act:tune`, `act:verbosity`, `act:more`, an unknown verb) through the REAL callback_query chain: listener.sh's own parsing/routing/ack, not just handle_action.sh's handler logic in isolation (already covered by `tests/test_dispatch.sh`). Doesn't touch spawn_claude.sh at all. |
| `test_menu_combinations.py` | Menu-button COMBINATIONS beyond sequential single taps: a stale, older menu message stays fully tappable independent of a newer screen drawn since; a rapid double-tap doesn't corrupt state; a `decor:` tap is genuinely inert; a menu action still works immediately while the room is mid-turn (not gated by busy state the way a plain message is); and confirms that a bare `act:cancel` tap kills an in-flight spawn immediately, with no confirm step (the documented one-tap behaviour, see Known findings below). |

## How a scenario is built

1. `harness.E2EHarness` copies `scripts/` and `hooks/` into a fresh temp
   dir and writes a fake `.env` there. `REPO_ROOT` in every script is
   computed as `dirname(BASH_SOURCE)/..` at runtime, so running
   `listener.sh` from the copy gets a fully isolated `state/`/`logs/` tree
   for free, no path-handling changes needed in the scripts themselves.
2. It starts `fake_telegram_server.py` on an ephemeral port and points the
   whole pipeline at it via `TELEGRAM_API_BASE` (an env var honoured by
   `listener.sh`, `tg.sh`, `stream_to_telegram.py`, `spawn_claude.sh`'s
   `ask_user` MCP config, `render_rooms.py`, `ask_user_mcp_server.py`,
   `register_commands.sh`, and `commands/users.sh`; defaults to the real
   endpoint everywhere, so production behaviour is unchanged when unset).
3. It points `spawn_claude.sh` at `fake_claude.py` via `CLAUDE_BIN_OVERRIDE`
   (checked before the real hardcoded binary path, so the fake wins even
   if a real `claude` is also installed on the machine).
4. It launches the sandboxed `listener.sh` as a real background process
   (own process group, so teardown can kill the whole tree including
   `spawn_claude.sh` and `fake_claude.py` descendants).
5. The test calls `h.inject_dm(text)` / `h.inject_group_message(chat_id,
   text)` / `h.inject_callback_query(data, chat_id)` to simulate incoming
   Telegram activity, and `h.wait_for_call(method, predicate, timeout=...)`
   to poll the fake server's recorded calls until the expected `sendMessage`
   / `editMessageText` / etc. shows up (raises with a dump of recent calls
   plus the tail of `spawn.log`/`listener.log` on timeout, so a failure is
   diagnosable without re-running with `KEEP_SANDBOX=1`). `h.wait_for_room_
   file(chat_id, name, predicate, timeout)` does the same for on-disk room
   state (sessions.json, .turns_done, .crash_times, etc.) when that's the
   more reliable signal than guessing which Telegram call carries it.

## Writing the fake claude's responses

`fake_claude.py` is driven by a JSON fixture. Turns can be scoped per room:

```json
{"rooms": {
    "555000111": {"turns": [{"text": "reply for this DM"}]},
    "-4900000001": {"turns": [{"text": "reply for this group"}]}
 },
 "turns": [{"text": "fallback for any room not listed above"}]}
```

Build one with `harness.write_fake_claude_script(path, turns)` for a single
flat list (used as the fallback for every room), or
`harness.write_fake_claude_rooms_script(path, {chat_id: turns, ...})` for a
genuinely multi-room fixture (see `test_multi_room_isolation.py`). Pass the
path to `h.setup(fake_claude_script=path)`.

In **persistent mode** (the normal room flow) turns are consumed one per
inbox line, in order; running out of scripted turns falls back to a generic
default reply instead of erroring, so a test only needs to script the turns
it actually cares about. Turn consumption is tracked PER ROOM in a small
state file next to the script (`<script>.state.<chat_id>`), so it survives
across separate `fake_claude.py` process launches (matters for rate-limit/
crash recovery, where the resumed spawn is a brand-new process) without one
room's spawn stealing another's turn.

Each turn can:
- `"text"` - emit a normal streamed text reply (message_start -> content
  deltas -> message_stop -> a clean `result` envelope).
- `"echo": true` - reply text IS the raw inbox line's message content,
  verbatim. Lets a test assert on EXACTLY what content/prefix reached
  claude (sender-name prefixing, `@mention` stripping, embedded newlines,
  unicode) instead of guessing from a canned reply. See
  `test_group_mention_gating.py` / `test_multi_user_group_conversation.py`.
- `"rate_limit": {"resets_in": N, "rate_limit_type": "..."}` plus
  `"exit_code"` - emit a `rate_limit_event` + an `is_error` result carrying
  429-shaped text, then exit with that code (mirrors a real 429 kill).
- `"crash": true` plus `"exit_code"` - exit immediately with no result
  envelope at all (mirrors a raw crash: OOM, network drop, internal error).
- `"auth_failure": {"text": "...", "api_error_status": "401"}` - emit a
  structured `is_error` result shaped like an API auth error. NOTE: this is
  NOT the documented real shape (see Known findings below) - for testing
  the actual documented path, just use a normal `"text"` turn whose body
  contains one of the three phrases `check_auth_failure()` matches on
  (`"invalid authentication credentials"`, `"failed to authenticate"`,
  `"invalid api key"`).
- `"hang": true` - emit a Bash tool-call marker (content_block_start ->
  delta -> content_block_stop for a `Bash` tool_use), then block forever
  via `signal.pause()` until an external SIGTERM/SIGKILL tears the process
  down. Models a genuinely long-running tool call, for testing `/cancel`
  against real in-flight work. Deliberately NOT a stalled text block: real
  claude streams text continuously once generation starts, so a stalled
  *text* delta with nothing following it doesn't happen in practice, and
  `stream_to_telegram.py`'s buffer flush only runs when a new event
  arrives (no independent timer) - a hanging text turn would just never
  render past its first chunk, which isn't what a real hang looks like.

## Known findings from building this suite

- **FIXED: the historical "rate-limit spam every 5 minutes" bug, root-
  caused.** `_park_room_for_rate_limit()` in `scripts/stream_to_telegram.py`
  computed `best_resets` from the cached rate_limit_event snapshot (the
  REAL reset time Anthropic reported), then threw it away: the final write
  always hardcoded `now_ts + 300` (5 minutes) regardless of what
  `best_resets` said. This write lands BEFORE spawn_claude.sh's own more
  accurate post-exit calculation, and spawn_claude.sh's idempotency guard
  treats an existing `.rate_limited_until` as authoritative once it looks
  "already parked" rather than always re-checking, so a real multi-hour
  rate limit got written as a 5-minute park. The room then retried in 5
  minutes, hit the SAME real limit again, wrote ANOTHER 5-minute park, and
  repeated indefinitely: exactly the "rate-limit message every 5 minutes"
  symptom that had previously been seen without the mechanism being
  found. Found when
  `test_multi_room_restart_survival.py` and `test_rate_limit_recovery.py`
  started flaking under the load of a full suite run: both tests'
  short (~10s) configured rate-limit windows came back as ~299s instead,
  which pointed straight at the always-300s write. Fixed by writing
  `best_resets` (falling back to `now_ts + 300` only when the cache
  genuinely has no usable entry, the one case that fallback is actually
  meant for). Covered by `tests/test_park_room_rate_limit.py` (fast unit
  test, three cases: a real multi-hour window, a real short window under
  5 minutes, and the genuine no-cache-entry fallback) so this can't
  regress silently again.
- **FIXED: queued messages silently lost every letter 'e' (also 'x', '1',
  and literal backslash), replaced with a newline.** This was the root
  cause of an earlier, unexplained "queued text arrives chopped up with
  letters missing" symptom. `drain_queue_if_idle()` in `scripts/listener.sh`
  coalesced queued messages with `tr '\x1e' '\n'`, intending `\x1e` as the
  single hex-escaped byte used to encode embedded newlines in the
  line-oriented queue file. BSD `tr` (as shipped with macOS) has no `\xHH`
  hex-escape support: it parsed `'\x1e'` as the four LITERAL characters
  `\`, `x`, `1`, `e` and mapped ANY of them to a newline, since `tr`
  treats its first argument as a character SET, not a string. Every
  queued message that went through this coalesce path (any message sent
  while the room was busy without a live inbox consumer, or while
  rate-limited) silently lost every occurrence of the letter 'e', the
  single most common letter in English. Found via
  `test_message_while_rate_limited.py`, whose echoed coalesced text came
  back as `'first m\nssag\n, will g\nt rat\n-limit\nd...'` instead of
  `'first message, will get rate-limited...'`. Fixed by switching to
  bash's own `${combined//$'\x1e'/$'\n'}` parameter expansion, the exact
  technique `listener.sh` already used safely for the identical
  substitution elsewhere in the same file (~line 1131); this call site
  was just the one inconsistent spot still piping through external `tr`.
  Covered going forward by `tests/test_queue_coalesce.sh` (fast, no E2E
  harness needed) as well as the E2E test that found it.
- **FIXED: structured-`is_error` auth failures used to produce 3
  redundant messages instead of 1** (and, as a side effect of the same
  fix, EVERY rate-limit hit used to send an extra redundant raw-text
  message too). `test_auth_failure_vs_rate_limit.py`'s second case
  confirmed: an auth failure arriving as `{"is_error": true,
  "api_error_status": "401", "result": "..."}` (distinct from the
  documented plain-text shape) made `stream_to_telegram.py` send (a) the
  raw result text verbatim via its "streamed nothing, but result has
  text" fallback, unconditional on whether the result was an error, (b) a
  generic `"⚠️ claude error: 401"` via `_fmt_error_result()` (since
  `check_auth_failure(raw_err)` checked `api_error_status`/`subtype`
  first, just `"401"`, which doesn't match any of the three auth
  phrases), and (c) the correct `"🚨 Bot OAuth token expired"` alert from
  a SECOND `check_auth_failure()` call that scans the full result text.
  Fixed by (1) gating the "streamed nothing" fallback to skip error
  results entirely, since the dedicated is_error branch already sends
  the right message, and (2) checking the FULL result text for auth
  phrases before deciding whether the generic fallback message is needed,
  not just the narrower `raw_err` string. Both cases in
  `test_auth_failure_vs_rate_limit.py` now assert exactly one message.
- **Resolved: bare `act:cancel` has no confirm step, and the docs now say
  so.** `scripts/commands/cancel.sh` never reads `$ARGS` and kills an
  in-flight spawn immediately on ANY `act:cancel` tap (`act:cancel yes`
  behaves identically). CLAUDE.md's `cancel` row used to describe a confirm
  prompt that the code no longer has; it now documents the one-tap
  behaviour. `test_menu_combinations.py`'s bare-cancel case asserts the
  one-tap behaviour, so reintroducing a confirm step would show up there.
- **The harness's own turn-tracking used to be a real bug, caught before
  it shipped.** The turn-index state file was originally keyed only by
  script path, not by room. A "nudge" message sent to force the listener's
  poll to return early would spawn its OWN `fake_claude.py` process reading
  from the same script/state file, silently stealing the next turn meant
  for the room actually under test. Fixed by keying the state file
  `<script>.state.<chat_id>` (see `fake_claude.py`'s docstring) and
  verified by `test_multi_room_isolation.py`, which is the regression test
  for this specific class of bug.
- **A "hang" fixture must model a stuck TOOL CALL, not stuck TEXT.** An
  earlier draft of the `/cancel` test emitted one partial text delta and
  then blocked forever, expecting the streamer to keep showing progress.
  It never did, because `stream_to_telegram.py` only re-evaluates its
  edit-throttle buffer when a NEW event arrives; with nothing further
  emitted there's nothing to trigger a flush. This is correct behaviour
  (a real claude stream doesn't stall mid-text either), but it means a
  "genuinely stuck" fixture needs to model the thing that actually stalls
  in a live bot: a long-running tool call, whose `content_block_stop`
  marker is sent as its own unbuffered message immediately, independent of
  the text-buffer throttle.

## Gotchas specific to this harness

- **A test's own timeout/text-size assumptions can be wrong even when the
  product code is right.** `test_long_reply_splitting.py`'s first draft
  waited on a `sendMessage` carrying a tail marker that was never going to
  arrive, because the fixture text happened to land just under the actual
  ~4000-char split threshold and finished as a single `editMessageText`
  instead. Prefer waiting on an unambiguous completion signal (like
  `.turns_done` via `wait_for_room_file`) over guessing which Telegram
  call type carries the content you're looking for.
- **The listener's poll timeout is dynamic**, 5s while any room is busy,
  30s while fully idle (`any_room_busy()` in `listener.sh`). A short
  rate-limit window in a fixture (`resets_in: 3`) can clear entirely within
  a single 5s short-poll cycle if a room was busy at that cycle's start, so
  a scenario shouldn't assume queueing state is still visible by the time
  it checks: poll, or treat "already drained because it already resumed"
  as an equally valid outcome. See the queue.jsonl check in
  `test_rate_limit_recovery.py` for the pattern.
- **Don't nudge the listener's poll with another dispatchable text
  message** if a scenario is mid-way through a scripted turn sequence for
  another room, UNLESS the fixture is a genuinely multi-room one built via
  `write_fake_claude_rooms_script` (each room then has its own turn
  cursor, so this is safe). For a single-room fixture, use a command that
  never reaches `spawn_claude.sh` instead (`/menu` routes to
  `handle_action.sh`, not the claude pipeline), see
  `test_rate_limit_recovery.py`.
- **BOT_DRAFT_STREAMING=0** is set in every scenario so far, forcing the
  plain edit-in-place path (`sendMessage` "…" placeholder, then
  `editMessageText` edits) instead of DM draft streaming. The fake server
  can technically handle `sendMessageDraft` calls too, but no scenario
  needs that complexity yet; drop the env var if a future test specifically
  wants to exercise draft mode.
- **Assert exact message COUNTS, not just "a matching message showed up",
  when redundancy/spam is part of what you're testing.** Several scenarios
  here (`test_no_admin_warning.py`'s no-duplicate check,
  `test_auth_failure_vs_rate_limit.py`'s exactly-one-alert check) exist
  because "did the right text appear somewhere" would have passed even
  with a duplicate-message bug sitting right next to it.
