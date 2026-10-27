#!/usr/bin/env python3
"""
capture_window - capture a single macOS window to PNG by CGWindowID.

Uses ``CGWindowListCreateImage`` so the capture works even when the
display is powered off or asleep. ``screencapture -l`` reads from the
rendered framebuffer and silently returns empty/black images when the
display is off; this script reads from the Window
Server's offscreen buffer instead and is unaffected by display state.

Usage:
    capture_window.py <window_id> <output_path>

Exits 0 on success, non-zero on failure with a short reason on stderr.
"""

from __future__ import annotations

import sys

try:
    from Quartz import (
        CGRectNull,
        CGWindowListCreateImage,
        kCGWindowImageBoundsIgnoreFraming,
        kCGWindowImageDefault,
        kCGWindowListOptionIncludingWindow,
    )
    from Quartz.ImageIO import (
        CGImageDestinationAddImage,
        CGImageDestinationCreateWithURL,
        CGImageDestinationFinalize,
    )
    from CoreFoundation import (
        CFURLCreateFromFileSystemRepresentation,
        kCFAllocatorDefault,
    )
except ImportError as e:
    sys.stderr.write(
        f"capture_window: import failed ({e}). Re-create the venv:\n"
        "  python3 -m venv .venv && "
        ".venv/bin/pip install pyobjc-framework-Quartz\n"
    )
    sys.exit(2)


def capture(window_id: int, out_path: str) -> int:
    image = CGWindowListCreateImage(
        CGRectNull,
        kCGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageBoundsIgnoreFraming | kCGWindowImageDefault,
    )
    if image is None:
        sys.stderr.write(
            f"capture_window: CGWindowListCreateImage returned None for "
            f"id={window_id}. Window may have closed, or Screen Recording "
            f"permission is missing for this process.\n"
        )
        return 3

    encoded = out_path.encode("utf-8")
    url = CFURLCreateFromFileSystemRepresentation(
        kCFAllocatorDefault, encoded, len(encoded), False)
    dest = CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if dest is None:
        sys.stderr.write(
            f"capture_window: CGImageDestinationCreateWithURL failed for "
            f"{out_path}\n")
        return 4
    CGImageDestinationAddImage(dest, image, None)
    if not CGImageDestinationFinalize(dest):
        sys.stderr.write(
            f"capture_window: CGImageDestinationFinalize failed for {out_path}\n")
        return 5
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("Usage: capture_window.py <window_id> <output_path>\n")
        return 1
    try:
        wid = int(argv[1])
    except ValueError:
        sys.stderr.write(f"capture_window: invalid window id '{argv[1]}'\n")
        return 1
    return capture(wid, argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
