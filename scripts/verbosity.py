#!/usr/bin/env python3
"""verbosity.py - read/write per-room verbosity settings.

Schema (state/rooms/<chat>/verbosity, JSON):
    {
      "tool_calls": "on" | "off",
      "edits":      "diff" | "marker" | "off",
      "thinking":   "on" | "off"
    }

Defaults (file absent OR key missing): tool_calls=on, edits=diff, thinking=on
This preserves the original full-noise behaviour for rooms that never opened
the verbosity screen.

Independence rule: the three keys gate three distinct outputs and don't cross
over. In particular `edits` controls BOTH the inline 📝 marker (in the live
message buffer) AND the separate diff message, on this scale:
    edits=diff   → marker AND separate diff
    edits=marker → marker only, no separate diff
    edits=off    → neither
`tool_calls` only affects NON-edit tool markers (⚙️/📖/🔎/...); it doesn't
silence edits because the user can express that with `edits=off`.

Used by:
    - scripts/commands/verbosity.sh: CLI driver for the menu screen
    - scripts/stream_to_telegram.py: imports load() to gate output per turn
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

DEFAULTS = {"tool_calls": "on", "edits": "diff", "thinking": "on"}
VALID = {
    "tool_calls": ("on", "off"),
    "edits":      ("diff", "marker", "off"),
    "thinking":   ("on", "off"),
}


def _path(repo_root: str | Path, chat_id: str | int) -> Path:
    return Path(repo_root) / "state" / "rooms" / str(chat_id) / "verbosity"


def load(repo_root: str | Path, chat_id: str | int) -> dict:
    """Return the room's verbosity dict, filling defaults for missing keys.

    Always returns a dict with the three canonical keys, so callers can use
    `v["tool_calls"]` etc. without `.get()`-with-default ceremony.
    """
    p = _path(repo_root, chat_id)
    try:
        raw = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in VALID and v in VALID[k]:
                out[k] = v
    return out


def save(repo_root: str | Path, chat_id: str | int, data: dict) -> None:
    p = _path(repo_root, chat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write only canonical keys + valid values so a corrupted save can't
    # poison future loads.
    clean = {k: data[k] for k in VALID if k in data and data[k] in VALID[k]}
    p.write_text(json.dumps(clean))


def set_key(repo_root: str | Path, chat_id: str | int,
            key: str, value: str) -> dict:
    if key not in VALID:
        raise ValueError(f"unknown verbosity key: {key!r}")
    if value not in VALID[key]:
        raise ValueError(
            f"invalid value for {key}: {value!r} "
            f"(allowed: {', '.join(VALID[key])})")
    data = load(repo_root, chat_id)
    data[key] = value
    save(repo_root, chat_id, data)
    return data


# ── Display labels for the menu screen ──────────────────────────────────────
LABELS = {
    "tool_calls": {"on": "shown", "off": "hidden"},
    "edits":      {"diff": "full diff", "marker": "marker only", "off": "hidden"},
    "thinking":   {"on": "shown", "off": "hidden"},
}
HEADERS = {
    "tool_calls": "🛠 Tool calls",
    "edits":      "📝 Edits",
    "thinking":   "💭 Thinking",
}
ORDER = ("tool_calls", "edits", "thinking")


def render_keyboard(data: dict) -> list:
    """Return the inline-keyboard 2D list for the verbosity screen.

    Layout per category:
        [<decor header>]
        [● activeValue]  [otherValue]  [otherValue]
    The "●" marker tells the user which value is currently set; tapping any
    button fires `act:verbosity <key> <value>` and the handler re-renders.
    """
    rows = []
    for key in ORDER:
        rows.append([{
            "text": HEADERS[key],
            "callback_data": f"decor:verbosity-{key}",
        }])
        rows.append([
            {
                "text": ("● " if data[key] == value else "") + LABELS[key][value],
                "callback_data": f"act:verbosity {key} {value}",
            }
            for value in VALID[key]
        ])
    return rows


def render_summary(data: dict) -> str:
    """One-line current-state summary for the screen body."""
    parts = [f"{HEADERS[k].split()[0]} {LABELS[k][data[k]]}" for k in ORDER]
    return " · ".join(parts)


def _cli():
    """Bash-facing CLI. Argv shape:
        verbosity.py get        <chat_id>
        verbosity.py set        <chat_id> <key> <value>
        verbosity.py render-kb  <chat_id>
        verbosity.py summary    <chat_id>
    """
    repo = Path(__file__).resolve().parent.parent
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    chat = sys.argv[2] if len(sys.argv) > 2 else ""
    if not chat:
        sys.stderr.write("verbosity.py: chat_id required\n")
        sys.exit(2)
    if cmd == "get":
        print(json.dumps(load(repo, chat)))
    elif cmd == "set":
        if len(sys.argv) < 5:
            sys.stderr.write("verbosity.py set <chat_id> <key> <value>\n")
            sys.exit(2)
        try:
            data = set_key(repo, chat, sys.argv[3], sys.argv[4])
        except ValueError as e:
            sys.stderr.write(f"verbosity.py: {e}\n")
            sys.exit(1)
        print(json.dumps(data))
    elif cmd == "render-kb":
        print(json.dumps(render_keyboard(load(repo, chat))))
    elif cmd == "summary":
        print(render_summary(load(repo, chat)))
    else:
        sys.stderr.write(
            "verbosity.py: unknown subcommand "
            "(use: get | set | render-kb | summary)\n")
        sys.exit(2)


if __name__ == "__main__":
    _cli()
