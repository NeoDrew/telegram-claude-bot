#!/usr/bin/env python3
"""
test_nested_fence_in_edit_tool.py - regression test for tool-preview
messages whose markdown rendering broke.

_format_edit_tool (stream_to_telegram.py) wraps a Write tool's whole file
content, or an Edit/MultiEdit's old_string/new_string, in one outer ```
fence. If that content already contains its own literal ``` (any markdown
file with a fenced example block, extremely common in docs), the message
ends up with 4+ backtick-fence markers. md_telegram.py's _FENCED_RE pairs
them in encounter order (1st-with-2nd, 3rd-with-4th) rather than
open-with-matching-close, so the middle of the message falls OUTSIDE any
code block (renders as plain, unindented text) while the tail gets wrongly
wrapped in a second, spurious <pre>.

Fixed by _neutralize_fences(): inserts a zero-width space after the first
backtick of every 3+ run in untrusted embedded content, before it's placed
inside the outer fence, so it can never be mistaken for a fence delimiter
by _FENCED_RE, while remaining visually identical to the reader.

Run: python3 tests/test_nested_fence_in_edit_tool.py
"""

import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import stream_to_telegram as streamer  # noqa: E402
import md_telegram  # noqa: E402


REPRO_CONTENT = """# Local document library (not committed)

Drop downloaded source documents here. Suggested layout:

```
library/
  vendor-docs/spec-v2.pdf
  reference/manual.pdf
  ...
```

Everything under this folder is git-ignored, see `../README.md` for why.
Tick the matching checkbox in `../sources.md` once a document is here.
"""


def main():
    failures = []

    # ---- _neutralize_fences itself ----
    if "```" in streamer._neutralize_fences("plain text, no fences"):
        pass  # trivially true, no fences to break
    neutralized = streamer._neutralize_fences("before\n```\ncode\n```\nafter")
    if "```" in neutralized:
        failures.append(f"_neutralize_fences left a literal ``` run intact: {neutralized!r}")
    elif neutralized.replace("​", "") != "before\n```\ncode\n```\nafter":
        failures.append(f"_neutralize_fences changed visible content, expected only a "
                         f"zero-width space inserted, got {neutralized!r}")
    else:
        failures_before = len(failures)
        print("ok: _neutralize_fences breaks a literal ``` run without changing "
              "what the reader sees")

    # ---- the real repro: a Write tool whose content has its own fence ----
    msg = streamer._format_edit_tool(
        "Write", {"file_path": "docs/library/README.md", "content": REPRO_CONTENT})
    html = md_telegram.md_to_telegram_html(msg)

    pre_count = html.count("<pre>")
    if pre_count != 1:
        failures.append(f"expected exactly 1 <pre> block for a Write whose content has one "
                         f"embedded fence, got {pre_count}. Full output:\n{html}")
    else:
        print("ok: a Write tool's embedded ``` no longer splits the message into two <pre> blocks")

    # "vendor-docs/spec-v2.pdf" only ever appears inside the directory
    # listing itself, unlike "library/" which also appears in the file
    # path header above the <pre> block, so this is an unambiguous marker.
    marker = "vendor-docs/spec-v2.pdf"
    if marker not in html:
        failures.append(f"the directory listing (previously leaked outside the <pre> block as "
                         f"unindented plain text) must still be present in the output:\n{html}")
    elif "<pre>" in html and html.index("<pre>") < html.index(marker) < html.index("</pre>"):
        print("ok: the directory listing stays INSIDE the single <pre> block, not leaked "
              "as plain text between two mis-paired fences")
    else:
        failures.append(f"the directory listing must be inside the <pre> block:\n{html}")

    # ---- Edit tool: old_string/new_string can carry a fence too ----
    edit_msg = streamer._format_edit_tool("Edit", {
        "file_path": "docs/example.md",
        "old_string": "old text",
        "new_string": "```\nfenced example\n```\nmore text",
    })
    edit_html = md_telegram.md_to_telegram_html(edit_msg)
    if edit_html.count("<pre>") != 1:
        failures.append(f"Edit tool with a fence in new_string: expected exactly 1 <pre>, "
                         f"got {edit_html.count('<pre>')}:\n{edit_html}")
    else:
        print("ok: an Edit tool's new_string containing its own ``` doesn't split the message")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nok: embedded fenced code blocks inside tool-preview content no longer "
          "break the outer Telegram message structure")


if __name__ == "__main__":
    main()
