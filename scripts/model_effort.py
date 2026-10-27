"""Single source of truth for the model catalogue and the model → effort
matrix. Imported by commands/effort.sh, commands/model.sh and
commands/tune.sh (their inline python blocks), so the gating can never
drift between the standalone pickers and the combined screen.

Effort levels are model-dependent:
  Fable 5.1 / Sonnet 5 / Opus 5.5 / Opus 5 / Opus 4.8
                                  -> low, medium, high, xhigh, max + ultracode
  Opus 4.7                       -> low, medium, high, xhigh, max
  Opus 4.6 / Sonnet 4.6         -> low, medium, high, max
  Haiku (and older)             -> no effort support
"ultracode" is a Claude Code session setting (sends xhigh AND auto-
orchestrates dynamic workflows), offered on the 5.x and 4.8 tier.

The "fable" alias resolves to the 5.1 id, but older per-room model files
still holding the bare "claude-fable-5" string keep working unchanged
(spawn_claude.sh passes MODEL_FILE's contents straight through to --model,
it never re-resolves via this table).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_config  # noqa: E402

# (button label, alias used in callback_data, full model id)
OPTIONS = [
    ("fable 5.1",    "fable",    "claude-fable-5-1"),
    ("sonnet 5",     "sonnet5",  "claude-sonnet-5"),
    ("opus 5.5",     "opus55",   "claude-opus-5-5"),
    ("opus 5",       "opus5",    "claude-opus-5"),
    ("opus 4.8 1M",  "opus1m",   "claude-opus-4-8[1m]"),
    ("opus 4.7",     "opus47",   "claude-opus-4-7"),
    ("sonnet 4.6",   "sonnet46", "claude-sonnet-4-6"),
    ("haiku",        "haiku",    "claude-haiku-4-5"),
]

# The model a room uses when it has no per-room override. BOT_DEFAULT_MODEL
# (env or .env) replaces it. spawn_claude.sh explicitly passes
# --model DEFAULT_MODEL whenever a room has no override, rather than
# omitting --model and relying on claude's own ambient default, so the
# "default" label the menus show is what actually gets spawned.
DEFAULT_MODEL = bot_config.get("BOT_DEFAULT_MODEL", "claude-opus-5-5")


def levels_for(mid):
    """Return ([effort levels], ultracode_eligible) for a model id."""
    if ("fable-5" in mid or "sonnet-5" in mid or "opus-4-8" in mid
            or "opus-5" in mid):
        return ["low", "medium", "high", "xhigh", "max"], True
    if "opus-4-7" in mid:
        return ["low", "medium", "high", "xhigh", "max"], False
    if "opus-4-6" in mid or "sonnet-4-6" in mid:
        return ["low", "medium", "high", "max"], False
    if "haiku" in mid:
        return [], False
    # Unknown / future model: offer the broad set, withhold ultracode.
    return ["low", "medium", "high", "xhigh", "max"], False


def display(mid):
    """Drop the redundant claude- prefix for button labels."""
    return mid[7:] if mid.startswith("claude-") else mid


def default_label():
    """Short label for DEFAULT_MODEL: its button label when it is in the
    catalogue, else the id without the claude- prefix."""
    for label, _alias, full in OPTIONS:
        if full == DEFAULT_MODEL:
            return label
    return display(DEFAULT_MODEL)
