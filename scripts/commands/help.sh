# commands/help.sh - handler for /help
# SOURCED by handle_action.sh (not executed standalone): runs in the
# dispatcher's shell, so REPO_ROOT, ROOM_DIR, SESSIONS_FILE, BUSY_LOCK,
# QUEUE_FILE, EFFORT_FILE, MODEL_FILE, SCHED_FILE, CMD, ARGS, CALLBACK_MSG_ID
# and lib.sh helpers (reply_kb, extract_var, kill_room_spawn, ...) are all
# in scope. Output the reply on stdout; the listener pipes it to tg.sh.

    cat <<'EOF'
**🤖 Claude Code, on your phone**

This bot is Claude Code running on your Mac. Message it the way you'd talk to Claude in a terminal: it works inside one project folder at a time, runs commands, reads and edits files, can drive Chrome (with the optional chromeflow plugin), and streams its answer back as it writes.

**What it can do**

- Hold a full coding conversation in a project, with history that persists between messages.
- Switch projects and start fresh sessions whenever you want.
- Stream replies live, and send screenshots of any window or a specific Chrome tab.
- Work from any chat: every DM, group, or topic is its own independent workspace.
- Show Claude's usage limits and exactly what it's doing right now.
- Run scheduled tasks, change the model and reasoning effort, and rotate its login.

**How to drive it**

Just type a message and it goes straight to Claude. Tap /menu for everything as buttons: status, switch folder, model, effort, screenshots, usage, scheduling, and more.

**Reading the chat**

- 🫡 your message is queued; Claude is busy and will get to it.
- 👀 Claude has read it and is answering. There is no extra reaction when the answer is done, so a long reply never leaves a notification on a message far up the chat.
- 😴 a usage limit is on; it will be answered automatically when the limit resets.
- ⏱ "Worked for…" after a reply: how long it took and how full the conversation's memory is. Near full it compresses itself, and shows 🗜 while it does.
- Buttons in a question from Claude: tap one to answer. They expire after 5 minutes.
- 🛑 cancel stops the current reply; anything you sent after it is still answered.
EOF
