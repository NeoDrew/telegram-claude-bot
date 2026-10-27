#!/usr/bin/env python3
"""
registry.py - project -> room binding enforcement, AUTO-LEARNED.

The registry is the bot's runtime memory of which project lives in which
Telegram room. Bindings are persisted in state/room_registry.json. They
are seeded automatically the first time a prompt or cwd identifies a new
project. Once seeded, future schedules from other rooms that mention the
same project are refused. No human edits are required to defend a new
project, and no chat ids are hardcoded anywhere in the bot.

Bindings auto-extracted on every schedule:

  * path_prefixes - any /Users/<u>/<workspace>/<project>/
    path found in the prompt is reduced to its project root and bound to
    the calling room's target_chat_id. The room's own active_cwd is
    reduced the same way and bound too, so even prompts that don't name
    the project explicitly will lock the binding after one schedule.
  * url_prefixes - any http(s):// URL is reduced to scheme://host/ and
    bound similarly.

Validation: on every subsequent schedule, the extracted prefixes are
looked up. If any matches an existing binding with a different
target_chat_id, the script REFUSES (exit 2) with an actionable message.
The user can edit state/room_registry.json to fix an incorrect binding
or use registry.py forget <prefix> to drop one.

Manual seeding remains supported: a project block with `name`,
`target_chat_id`, and `path_prefixes`/`url_prefixes` continues to be
honoured exactly as before. Manual entries are never auto-modified; only
new prefixes are appended to auto-learned entries.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "state" / "room_registry.json"

# /Users/<u>/<workspace>/<project>/ . Common workspace folder names, plus
# whatever BOT_PROJECTS_DIR points at, so a custom projects folder is
# recognised with no code change.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import bot_config as _bot_config  # noqa: E402

_WORKSPACE_DIRS = tuple(dict.fromkeys(
    ("projects", "dev", "Documents", "Desktop", "code", "work", "src")
    + ((_os.path.basename(_bot_config.projects_dir().rstrip("/")),)
       if _os.path.basename(_bot_config.projects_dir().rstrip("/")) else ())
))

_PATH_RE = re.compile(
    r"/Users/([A-Za-z0-9._-]+)/("
    + "|".join(re.escape(d) for d in _WORKSPACE_DIRS)
    + r")/([A-Za-z0-9._-]+)(?:/|\b)"
)

_URL_RE = re.compile(r"(https?://[A-Za-z0-9._-]+(?::\d+)?)(/|$)")


def load_registry(path: Optional[Path] = None) -> dict:
    p = path or REGISTRY_PATH
    if not p.exists():
        return {"projects": []}
    try:
        with open(p) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"projects": []}
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return {"projects": []}
    return data


def save_registry(reg: dict, path: Optional[Path] = None) -> None:
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(reg, f, indent=2)


def extract_prefixes(prompt: str, cwd: Optional[str] = None) -> tuple[set[str], set[str]]:
    """Return (path_prefixes, url_prefixes) parsed out of prompt + cwd.

    Path prefixes are normalised to /Users/<u>/<workspace>/<project>/ so
    every reference into the same project collapses to one binding key
    regardless of subdirectory depth.

    URL prefixes are normalised to scheme://host/ for the same reason -
    different paths under the same host bind together.
    """
    paths: set[str] = set()
    urls: set[str] = set()

    haystack = prompt or ""
    for m in _PATH_RE.finditer(haystack):
        paths.add(f"/Users/{m.group(1)}/{m.group(2)}/{m.group(3)}/")
    for m in _URL_RE.finditer(haystack):
        urls.add(m.group(1) + "/")

    if cwd:
        m = _PATH_RE.match(cwd if cwd.endswith("/") else cwd + "/")
        if m:
            paths.add(f"/Users/{m.group(1)}/{m.group(2)}/{m.group(3)}/")

    return paths, urls


def _find_binding(prefix: str, reg: dict) -> Optional[dict]:
    """Return the project entry that owns `prefix`, or None."""
    for proj in reg.get("projects", []):
        if not isinstance(proj, dict):
            continue
        all_pre = (proj.get("path_prefixes") or []) + (proj.get("url_prefixes") or [])
        if prefix in all_pre:
            return proj
    return None


def match_project(prompt: str, cwd: Optional[str] = None,
                  registry: Optional[dict] = None) -> Optional[dict]:
    """Return the first project entry whose prefixes hit the prompt or cwd.

    Substring match against any registered prefix (path or URL). Kept
    permissive on purpose: a prompt that pastes a deep subpath should
    still match the root-level binding.
    """
    reg = registry if registry is not None else load_registry()
    haystack = prompt or ""
    cwd_norm = (cwd.rstrip("/") + "/") if cwd else ""
    for proj in reg.get("projects", []):
        if not isinstance(proj, dict):
            continue
        for pre in (proj.get("path_prefixes") or []) + (proj.get("url_prefixes") or []):
            if not pre:
                continue
            if pre in haystack:
                return proj
            if cwd_norm and (cwd_norm.startswith(pre) or pre in cwd_norm):
                return proj
    return None


def validate_target(prompt: str, target_chat_id: str,
                    cwd: Optional[str] = None,
                    registry: Optional[dict] = None) -> tuple[bool, str]:
    """Returns (ok, message). ok=False means: REFUSE THIS SCHEDULE.

    Two layers: (1) the legacy match_project pass for manually-seeded
    bindings; (2) auto-extraction of prefixes from prompt+cwd to catch
    any binding regardless of name. Both must agree (or be silent) for a
    schedule to pass.
    """
    reg = registry if registry is not None else load_registry()

    proj = match_project(prompt, cwd=cwd, registry=reg)
    if proj is not None:
        required = str(proj.get("target_chat_id", "")).strip()
        if required and str(target_chat_id).strip() != required:
            return False, (
                f"REGISTRY REFUSED: prompt matches project "
                f"{proj.get('name', '?')!r} which is bound to "
                f"target_chat_id={required}, but caller asked for "
                f"target_chat_id={target_chat_id}. "
                f"Edit state/room_registry.json or run "
                f"`scripts/registry.py forget <prefix>` to rebind."
            )

    # Auto-extraction layer: even if no named project matched, scan the
    # prompt+cwd for any prefix that's been seen before. Catches the
    # case where a new project's binding was auto-learned earlier from a
    # different prompt that didn't carry the project's name.
    paths, urls = extract_prefixes(prompt or "", cwd=cwd)
    for pre in paths | urls:
        owner = _find_binding(pre, reg)
        if owner is None:
            continue
        required = str(owner.get("target_chat_id", "")).strip()
        if required and str(target_chat_id).strip() != required:
            return False, (
                f"REGISTRY REFUSED: prefix {pre!r} is already bound to "
                f"target_chat_id={required} (project "
                f"{owner.get('name', '?')!r}), but caller asked for "
                f"target_chat_id={target_chat_id}. "
                f"This binding was auto-learned. Edit "
                f"state/room_registry.json or run "
                f"`scripts/registry.py forget {pre!r}` to rebind."
            )

    return True, "allowed"


def learn(prompt: str, target_chat_id: str, cwd: Optional[str] = None,
          registry_path: Optional[Path] = None) -> dict:
    """Persist any new prefixes from prompt+cwd as bound to target_chat_id.

    Idempotent: re-running with the same inputs adds nothing. Never
    overwrites existing bindings. If a prefix is already owned by a
    DIFFERENT target_chat_id, this function leaves it alone (validate
    would have refused upstream; learn is called only after validate
    succeeds, so collisions here would mean a race - rare and harmless
    because validation will refuse on the next schedule).

    Returns the updated registry dict.
    """
    reg = load_registry(registry_path)
    paths, urls = extract_prefixes(prompt or "", cwd=cwd)
    if not paths and not urls:
        save_registry(reg, registry_path)
        return reg

    target = str(target_chat_id).strip()
    if not target:
        return reg

    # Find or create the auto-learned entry for this target_chat_id.
    auto_entry = None
    for proj in reg["projects"]:
        if not isinstance(proj, dict):
            continue
        if (proj.get("target_chat_id") == target
                and proj.get("auto_learned") is True):
            auto_entry = proj
            break
    if auto_entry is None:
        auto_entry = {
            "name": f"auto:{target}",
            "target_chat_id": target,
            "auto_learned": True,
            "path_prefixes": [],
            "url_prefixes": [],
        }
        reg["projects"].append(auto_entry)

    def _append_if_new(prefix: str, bucket: str) -> None:
        owner = _find_binding(prefix, reg)
        if owner is not None:
            return  # someone owns it already (manual or auto); leave alone
        auto_entry.setdefault(bucket, []).append(prefix)

    for p in sorted(paths):
        _append_if_new(p, "path_prefixes")
    for u in sorted(urls):
        _append_if_new(u, "url_prefixes")

    save_registry(reg, registry_path)
    return reg


def forget(prefix: str, registry_path: Optional[Path] = None) -> bool:
    """Drop `prefix` from every project entry. Returns True if anything
    changed. Used to correct a mis-learned binding without hand-editing
    JSON."""
    reg = load_registry(registry_path)
    changed = False
    for proj in reg.get("projects", []):
        if not isinstance(proj, dict):
            continue
        for bucket in ("path_prefixes", "url_prefixes"):
            arr = proj.get(bucket) or []
            keep = [p for p in arr if p != prefix]
            if len(keep) != len(arr):
                proj[bucket] = keep
                changed = True
    if changed:
        save_registry(reg, registry_path)
    return changed


def _cli() -> int:
    if len(sys.argv) < 2:
        print("usage:\n"
              "  registry.py check <target_chat_id> <prompt> [cwd]\n"
              "  registry.py resolve <prompt> [cwd]\n"
              "  registry.py learn <target_chat_id> <prompt> [cwd]\n"
              "  registry.py forget <prefix>\n"
              "  registry.py list",
              file=sys.stderr)
        return 1

    sub = sys.argv[1]
    if sub == "resolve":
        prompt = sys.argv[2] if len(sys.argv) > 2 else ""
        cwd = sys.argv[3] if len(sys.argv) > 3 else None
        proj = match_project(prompt, cwd=cwd)
        if proj and proj.get("target_chat_id"):
            print(str(proj["target_chat_id"]).strip())
        return 0

    if sub == "check":
        if len(sys.argv) < 4:
            print("usage: registry.py check <target_chat_id> <prompt> [cwd]",
                  file=sys.stderr)
            return 1
        target = sys.argv[2]
        prompt = sys.argv[3]
        cwd = sys.argv[4] if len(sys.argv) > 4 else None
        ok, msg = validate_target(prompt, target, cwd=cwd)
        print(msg, file=sys.stderr)
        return 0 if ok else 2

    if sub == "learn":
        if len(sys.argv) < 4:
            print("usage: registry.py learn <target_chat_id> <prompt> [cwd]",
                  file=sys.stderr)
            return 1
        target = sys.argv[2]
        prompt = sys.argv[3]
        cwd = sys.argv[4] if len(sys.argv) > 4 else None
        learn(prompt, target, cwd=cwd)
        return 0

    if sub == "forget":
        if len(sys.argv) < 3:
            print("usage: registry.py forget <prefix>", file=sys.stderr)
            return 1
        ok = forget(sys.argv[2])
        print(f"forget {sys.argv[2]!r}: {'removed' if ok else 'no match'}",
              file=sys.stderr)
        return 0

    if sub == "list":
        reg = load_registry()
        for proj in reg.get("projects", []):
            tag = "auto" if proj.get("auto_learned") else "manual"
            print(f"[{tag}] {proj.get('name','?')} -> "
                  f"chat={proj.get('target_chat_id','?')}")
            for p in (proj.get("path_prefixes") or []):
                print(f"   path: {p}")
            for u in (proj.get("url_prefixes") or []):
                print(f"   url:  {u}")
        return 0

    print(f"unknown subcommand: {sub}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
