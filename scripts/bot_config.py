"""Machine-specific settings for the Python side of the bot.

Each value comes from the environment first, then a KEY=value line in the
repo's .env (read line by line, never bulk-sourced), then a default. The
bash side has the same lookup in lib.sh (bot_config and friends); keep the
keys and defaults of the two in step. Every key is listed in .env.example.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def get(key: str, default: str = "") -> str:
    value = os.environ.get(key, "")
    if not value:
        env = REPO_ROOT / ".env"
        try:
            for line in env.read_text().splitlines():
                if line.startswith(key + "="):
                    value = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    return value or default


def projects_dir() -> str:
    """Where the cd menus look for projects, and a room's default cwd.
    Defaults to the home folder; set BOT_PROJECTS_DIR to your projects folder."""
    return os.path.expanduser(get("BOT_PROJECTS_DIR", "~"))


def notify_cmd() -> str:
    """The phone notifier. Ships in the repo as scripts/notify-cmd."""
    return os.path.expanduser(get("NOTIFY_CMD", str(REPO_ROOT / "scripts" / "notify-cmd")))


def bot_name() -> str:
    """The assistant's display name (system prompt and bot messages)."""
    return get("BOT_NAME", "Claude")
