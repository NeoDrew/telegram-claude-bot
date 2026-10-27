#!/usr/bin/env python3
"""
enum_windows - list visible user-facing macOS windows.

Filters to ``kCGWindowLayer == 0`` (normal app windows). Skips system
chrome (Control Centre, menubar, Notification Center widgets) and floating
windows (floating reminder windows, etc.) which would only add noise to a screenshot
album.

Output: one window per line, TAB-separated::

    <window_id>\t<owner>\t<title>

Width and height are checked too: windows under 50×50 are skipped because
they tend to be invisible helper windows (some apps keep a 1×1 background
window around for IPC).

Requires PyObjC's Quartz framework. Install via the repo's venv::

    .venv/bin/pip install pyobjc-framework-Quartz
"""

from __future__ import annotations

import sys

try:
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    )
except ImportError:
    sys.stderr.write(
        "enum_windows: PyObjC Quartz framework missing. Run:\n"
        "  .venv/bin/pip install pyobjc-framework-Quartz\n"
    )
    sys.exit(2)


MIN_WIDTH = 50
MIN_HEIGHT = 50


def list_user_windows() -> list[dict]:
    opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    raw = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or []
    out: list[dict] = []
    for w in raw:
        if w.get("kCGWindowLayer") != 0:
            continue
        bounds = w.get("kCGWindowBounds") or {}
        if (bounds.get("Width", 0) < MIN_WIDTH
                or bounds.get("Height", 0) < MIN_HEIGHT):
            continue
        wid = w.get("kCGWindowNumber")
        if wid is None:
            continue
        owner = (w.get("kCGWindowOwnerName") or "").strip() or "?"
        title = (w.get("kCGWindowName") or "").strip()
        out.append({"id": int(wid), "owner": owner, "title": title})
    return out


def main() -> int:
    windows = list_user_windows()
    for w in windows:
        # Sanitise tabs/newlines in title so the TSV row parser stays simple.
        title = w["title"].replace("\t", " ").replace("\n", " ")
        owner = w["owner"].replace("\t", " ").replace("\n", " ")
        print(f"{w['id']}\t{owner}\t{title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
