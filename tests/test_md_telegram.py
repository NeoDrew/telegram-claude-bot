#!/usr/bin/env python3
"""Unit tests for scripts/md_telegram.py, markdown -> Telegram HTML.

This converter is on the hot path of every streamed reply and every
slash-command reply (both stream_to_telegram.py and tg.sh call it). A bug
here either mangles formatting or, worse, emits HTML that Telegram rejects
with a 400 (so the user sees nothing). The escaping tests are the important
ones: any '<', '>', '&' outside a generated tag MUST be entity-escaped or
Telegram's HTML parser breaks.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from md_telegram import md_to_telegram_html as md  # noqa: E402


class TestInlineFormatting(unittest.TestCase):
    def test_bold(self):
        self.assertEqual(md("**bold**"), "<b>bold</b>")

    def test_italic_star_and_underscore(self):
        self.assertEqual(md("*it*"), "<i>it</i>")
        self.assertEqual(md("_it_"), "<i>it</i>")

    def test_inline_code(self):
        self.assertEqual(md("`code`"), "<code>code</code>")

    def test_strikethrough(self):
        self.assertEqual(md("~~gone~~"), "<s>gone</s>")

    def test_heading_becomes_bold(self):
        self.assertEqual(md("# Heading"), "<b>Heading</b>")

    def test_bullet_becomes_dot(self):
        self.assertEqual(md("- item"), "• item")

    def test_mixed_inline(self):
        out = md("a **b** and `c`")
        self.assertIn("<b>b</b>", out)
        self.assertIn("<code>c</code>", out)


class TestEscaping(unittest.TestCase):
    """The security/robustness core: raw HTML metacharacters must not leak."""

    def test_angle_and_amp_escaped(self):
        self.assertEqual(md("plain <b> & x"), "plain &lt;b&gt; &amp; x")

    def test_lt_escaped(self):
        self.assertEqual(md("a < b"), "a &lt; b")

    def test_no_raw_unescaped_amp(self):
        out = md("Tom & Jerry < 5 > 3")
        # No bare & that isn't part of an entity.
        import re
        self.assertIsNone(re.search(r"&(?!(amp|lt|gt|quot|#\d+);)", out))

    def test_code_content_is_escaped(self):
        # Inside <code>, angle brackets still must be escaped for Telegram.
        out = md("`<script>`")
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    def test_markdown_link_becomes_a_tag(self):
        # <a href> IS one of Telegram's supported HTML tags (see this
        # module's own docstring), a well-formed [label](http(s)://url)
        # converts to a real, safely-escaped <a> so it renders as a clean
        # clickable link instead of literal bracket/paren/URL text.
        # Regression test: this used to assert the OPPOSITE (links must
        # NEVER become <a>) out of an unfounded worry about breaking the
        # parser, but every WebSearch "Sources:" list is built entirely
        # out of these links, so they rendered as raw markdown; now they
        # render as clickable links.
        out = md("[x](https://y.com)")
        self.assertIn('<a href="https://y.com">x</a>', out)

    def test_link_url_and_label_are_escaped(self):
        # The href attribute and the visible label are escaped
        # independently, an "&" in a query string must not break out of
        # the attribute, and "<"/">" in a label must not inject markup.
        out = md("[A & <B>](https://y.com/?a=1&b=2)")
        self.assertIn('href="https://y.com/?a=1&amp;b=2"', out)
        self.assertIn("A &amp; &lt;B&gt;", out)
        self.assertNotIn("<B>", out)


class TestRobustness(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(md(""), "")

    def test_plain_text_unchanged(self):
        self.assertEqual(md("just words"), "just words")

    def test_unbalanced_markers_do_not_crash(self):
        for s in ["**bold", "`code", "~~s", "*", "_ _ _", "###", "|||", "[("]:
            md(s)  # must not raise

    def test_fenced_code_block(self):
        out = md("```\nx=1\n```")
        self.assertIn("<pre>", out)
        self.assertIn("x=1", out)

    def test_fenced_code_escapes_html(self):
        out = md("```\n<tag> & y\n```")
        self.assertIn("&lt;tag&gt;", out)
        self.assertIn("&amp;", out)

    def test_idempotent_on_plain(self):
        once = md("hello world")
        self.assertEqual(md(once), once)

    def test_long_input_does_not_crash(self):
        md("**x** " * 5000)


class TestPipeTables(unittest.TestCase):
    """ALL tables reflow into proportional-font lines. Never <pre>: Telegram
    renders monospace smaller than body text and wraps wide grids into
    fragments, both of which read badly on a phone."""

    def test_small_table_reflows_to_key_value(self):
        src = "| A | B |\n|---|---|\n| x | y |"
        got = md(src)
        self.assertNotIn("<pre>", got)
        self.assertIn("• <b>x:</b> y", got)

    def test_key_cell_emphasis_stripped(self):
        src = "| **A** | B |\n|---|---|\n| **x** | y |"
        got = md(src)
        self.assertNotIn("**", got)
        self.assertIn("• <b>x:</b> y", got)

    def test_wide_two_col_becomes_key_value_list(self):
        src = ("| Setting | Value |\n|---|---|\n"
               "| Draft streaming interval for DMs | 0.25 seconds between frames |")
        got = md(src)
        self.assertNotIn("<pre>", got)
        self.assertIn("• <b>Draft streaming interval for DMs:</b> "
                      "0.25 seconds between frames", got)

    def test_wide_three_col_becomes_labelled_lines(self):
        src = ("| Product | Price | Notes |\n|---|---|---|\n"
               "| Widget | $4.50 | Blue, ships in a padded envelope |")
        got = md(src)
        self.assertNotIn("<pre>", got)
        self.assertIn("<b>Widget</b> · Price: $4.50 · "
                      "Notes: Blue, ships in a padded envelope", got)

    def test_wide_cell_markdown_renders(self):
        src = ("| Product | Price | Notes |\n|---|---|---|\n"
               "| **Widget** | $4.50 | stock `empty` and waiting for restock |")
        got = md(src)
        self.assertIn("<b>Widget</b>", got)
        self.assertIn("<code>empty</code>", got)
        self.assertNotIn("**", got)

    def test_wide_cell_html_is_escaped(self):
        src = ("| Name | Detail | Extra |\n|---|---|---|\n"
               "| a<b>c | uses & and <pre> tags everywhere | filler text |")
        got = md(src)
        self.assertIn("&lt;pre&gt;", got)
        self.assertIn("&amp;", got)

    def test_surrounding_prose_survives(self):
        src = ("intro line\n\n"
               "| Setting | Value |\n|---|---|\n"
               "| Draft streaming interval for DMs | 0.25 seconds between frames |\n"
               "\noutro line")
        got = md(src)
        self.assertIn("intro line", got)
        self.assertIn("outro line", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
