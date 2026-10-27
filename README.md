# telegram-claude-bot

Phone-to-Mac bridge for Claude Code. Message a Telegram bot, it relays to a
persistent Claude Code conversation running on your Mac and streams the reply
back live. Every Telegram chat (DM or group) it's added to becomes its own
independent "room" with its own working directory, session, and settings, so
you can run several project conversations in parallel from different chats.

## Using it

The bot is **menu-driven, not command-driven**. Send `/menu` (or `/start` in
a fresh chat) to get an inline-keyboard menu; everything happens by tapping
buttons: switching project directory, resetting the session, tuning
model/effort, capturing screenshots, managing scheduled tasks, approving
other users in a room, and so on. Only `/menu` and `/start` are handled by
the bot itself; any other typed slash command is passed to Claude as plain
text, so Claude Code's own slash commands and skills (for example `/compact`)
work when typed into the chat.

Plain text (no leading `/`) is sent straight to Claude as the next turn of
that room's conversation, and the reply streams back in place as it's
generated. Message while a reply is already in flight and it lands at the
next natural pause instead of getting lost.

## What it can do

- **Persistent, multi-turn conversations per room**, not one-shot spawns;
  effort, model, and tool state carry across messages in the same chat.
- **Switch project directories** via the menu's folder picker, including
  drilling into subfolders of a monorepo, without ever typing a path.
- **Automatic rate-limit recovery**: if a Claude rate limit is hit (the
  5-hour or the weekly window), the room parks itself, tells you when it'll resume, and picks back up
  automatically once the limit clears.
- **Scheduled/cron tasks**: one-off or recurring prompts that fire into a
  specific room at a specific time, routed by an explicit target chat id so
  a task can never land in the wrong conversation.
- **Window and tab screenshots** sent straight to the chat so you can see
  what's on screen, plus optional real browser control if you install a
  browser-automation MCP plugin such as chromeflow.
- **Per-room user approval**: the owner can let specific other Telegram
  users talk to the bot in one room without sharing credentials.
- **An alerts channel** the bot can escalate to when it's genuinely stuck
  and needs your attention, separate from routine chat traffic.

## What's running

A launchd daemon (label set by `BOT_LAUNCHD_LABEL` in `.env`, default `com.telegram-claude-bot`) running
`scripts/listener.sh`, long-polling Telegram and dispatching each room's
messages to its own Claude Code process. `KeepAlive=true`, so it restarts
automatically if it crashes. Logs in `logs/`.

Per-room state (session id, active directory, queue, busy lock, effort/model)
lives under `state/rooms/<chat_id>/`. `state/` and `logs/` are both
gitignored.

Optionally, local scheduled jobs that aren't tied to a chat can live in a
`scheduled/` folder at the repo root; it is gitignored, so anything there
stays on your machine.

## Requirements

macOS only for now (launchd, plus Quartz for the screenshot tools).

- **Claude Code CLI,** logged in on the Mac that runs the bot. Default path
  `~/.local/bin/claude`, or anywhere on `PATH`.
- **python3 and curl.** Homebrew's python3 is fine.
- **Node,** for Claude Code's plugin hooks. `install.sh` finds it on `PATH`
  or under `~/.nvm`.
- **Optional:** a browser-automation MCP plugin (for example chromeflow and
  its Chrome extension), for browser tasks.

## Setup

1. Create a bot via @BotFather, copy the token, and message it once.
2. Generate a long-lived Claude OAuth token: `claude setup-token`.
3. `cp .env.example .env`, then fill in `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID` and `CLAUDE_CODE_OAUTH_TOKEN`.
4. `chmod 600 .env`.
5. `./scripts/install.sh`. It writes and loads the launchd plist, builds
   `.venv` for the screenshot tools, and links `notify-cmd` into
   `~/.local/bin`.

Verify with `launchctl list | grep telegram-claude-bot`, then message the
bot and send `/menu`.

## Configuration

Everything machine-specific is a setting. Each one is read from the
environment first, then from `.env`, then falls back to a default, so the
three tokens above are the only required values. `.env.example` lists all of
them with comments:

- **`BOT_NAME`:** the name the assistant uses for itself in chat. Default
  `Claude`.
- **`BOT_PERSONA`:** an optional persona or tone, appended to the system
  prompt. Default: none.
- **`BOT_DEFAULT_MODEL`:** the Claude model a chat uses until it picks one
  in the menu. Default `claude-opus-5-5`.
- **`BOT_PROJECTS_DIR`:** where the cd menus look for projects, and each
  chat's default working directory. Default: your home folder.
- **`BOT_LAUNCHD_LABEL`:** the launchd job name. Default
  `com.telegram-claude-bot`; the dashboard job adds `-dashboard`.
- **`BOT_PRIMARY_HOST`:** pins the listener to one machine, since Telegram
  allows only one poller per bot token. Default: not pinned.
- **`NOTIFY_CMD`:** the phone notifier. Default `scripts/notify-cmd`.
- **`BOT_PYTHON`:** the python3 used to build `.venv`. Default: first on `PATH`.
- **`CLAUDE_BIN_OVERRIDE`:** the Claude Code binary.
- **`TELEGRAM_API_BASE`:** a self-hosted Bot API server, which lifts the
  20 MB download limit.
- **`TELEGRAM_ALLOWED_IDS`:** extra users trusted in every chat. Prefer the
  per-chat Users menu.
- **`BOT_DASHBOARD_PORT`:** the local metrics dashboard port. Default 8787.
- **`RL_ALERT_DEDUP_S`:** how long one rate-limit alert suppresses
  duplicates from other chats hitting the same limit. Default 7200 seconds.
- **`BOT_PRECOMPACT_PCT`:** context fill at which a new message is preceded
  by a compression, so Claude compresses first and then answers. A chat's
  own auto compact setting wins. Default 90.

Fixed on purpose: files people send the bot are saved to `~/Downloads`, and
per-chat state lives in `state/` inside the repo.

## Architecture, file layout, gotchas

See `CLAUDE.md` in this repo. It's the detailed architecture doc a Claude
Code session reads first when working here, and it stays current: state
shapes, the full menu/action reference, known footguns, and the safe-editing
rules for a bot that's allowed to modify its own running code.

## Caveats

- **Security.** The bot has shell access on the host Mac via
  `--dangerously-skip-permissions`. Only the allowlisted chat/user ids are
  answered; everyone else is silently dropped (or captured as "pending" for
  the owner to approve, per room). Don't share the bot token; if it leaks,
  BotFather `/revoke` + `/token` to mint a new one and update `.env`.
- **Single listener instance.** Only one `listener.sh` process should ever
  poll this bot's token, Telegram rejects concurrent `getUpdates` pollers.
  Restart via the menu's ♻️ button or `scripts/safe_restart.sh`, never by
  hand-launching a second process.
- **Network drops.** If the host briefly can't reach `api.telegram.org`, the
  listener retries; messages queue at Telegram's end and land once the
  connection recovers.
- **Browser plugin attachment.** If you use chromeflow, browser tasks only
  work when its extension is attached to a Chrome window; otherwise the
  plugin reports "not attached" rather than hanging. Every chat shares the
  same browser, so avoid driving it from two chats at once.
- **`/cd` to a different directory drops the room's session by design**,
  switching projects mid-conversation almost never wants to resume the old
  context. Start a new Telegram group per long-lived project conversation
  instead of relying on per-directory resume.
