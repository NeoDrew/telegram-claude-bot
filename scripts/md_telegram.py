#!/usr/bin/env python3
"""
md_telegram: convert a useful subset of Markdown into Telegram-flavoured HTML
(parse_mode=HTML). Used by both stream_to_telegram.py (live-streamed reply
edits) and tg.sh (slash-command replies, notify-cmd), so the rendering is
consistent across every code path that pushes text into the chat.

Telegram HTML supports a small tag set:
  <b> <i> <s> <code> <pre> <a href> <u> <tg-spoiler>
Anything else is rejected with a 400 from the API. So this converter:

  - Detects pipe tables and re-renders them inside <pre> with aligned columns
    (the previous behaviour was to send the raw `|---|---|` separators, which
    Telegram cannot render as a table; they showed up as a mess of pipes).
  - Escapes ALL other content with html.escape after extracting recognized
    markdown patterns, so stray angle brackets and ampersands cannot derail
    Telegram's HTML parser.
  - Is robust to partial input (mid-stream chunks): unclosed **bold** or `code`
    is left as literal text rather than throwing, so editMessageText keeps
    succeeding while text streams in.

The set of supported patterns is small on purpose. Telegram does not render
real tables, lists, or headings, and trying to fake those with nested HTML
adds parser-failure modes for almost no visible win.
"""

from __future__ import annotations

import html
import re

# Sentinel chars survive html.escape (escape only touches & < > " '),
# so we can carve out converted tags before escaping the rest, then put the
# real HTML tags back at the end.
_BOLD_OPEN  = "\x01B\x01"; _BOLD_CLOSE  = "\x01b\x01"
_ITAL_OPEN  = "\x01I\x01"; _ITAL_CLOSE  = "\x01i\x01"
_STRK_OPEN  = "\x01S\x01"; _STRK_CLOSE  = "\x01s\x01"
_QUOTE_OPEN = "\x01Q\x01"; _QUOTE_CLOSE = "\x01q\x01"
_PH_OPEN    = "\x02P"
_PH_CLOSE   = "\x02"

# Telegram caps text at 4096 chars per sendMessage; keep some headroom so the
# truncation marker fits.
MAX_CHARS_DEFAULT = 4000


def md_to_telegram_html(text: str, max_chars: int = MAX_CHARS_DEFAULT) -> str:
    """Convert ``text`` (markdown subset) into Telegram-flavoured HTML."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n...(truncated)"

    placeholders: list[str] = []

    def stash(html_content: str) -> str:
        idx = len(placeholders)
        placeholders.append(html_content)
        return f"{_PH_OPEN}{idx}{_PH_CLOSE}"

    text = _extract_fenced_code(text, stash)
    # Before everything else that works line by line, so the quoted lines
    # (with their "> " stripped) still get bullets, bold, links and so on.
    text = _convert_blockquotes(text)
    text = _convert_pipe_tables(text, stash)
    text = _extract_inline_code(text, stash)
    text = _extract_links(text, stash)
    # Math conversion runs AFTER code extraction (so $ inside `code` is left
    # alone) but BEFORE bold/italic (so $x_i$ doesn't get italic-mangled on
    # the underscore). The output is pure Unicode, no HTML, so escape and
    # placeholders downstream work normally.
    text = _convert_math(text)
    text = _convert_bold(text)
    text = _convert_strikethrough(text)
    text = _convert_italic(text)
    text = _convert_bullets(text)
    text = _convert_headings(text)
    text = _strip_hrules(text)

    text = html.escape(text)

    text = (text
        .replace(_BOLD_OPEN, "<b>").replace(_BOLD_CLOSE, "</b>")
        .replace(_ITAL_OPEN, "<i>").replace(_ITAL_CLOSE, "</i>")
        .replace(_STRK_OPEN, "<s>").replace(_STRK_CLOSE, "</s>")
        .replace(_QUOTE_OPEN, "<blockquote>").replace(_QUOTE_CLOSE, "</blockquote>"))

    for idx, ph_html in enumerate(placeholders):
        text = text.replace(f"{_PH_OPEN}{idx}{_PH_CLOSE}", ph_html)

    return text


# ---------------------------------------------------------------------------
# Stage helpers (each takes the in-progress text + the stash callback, returns
# the transformed text). Kept small and individually testable.
# ---------------------------------------------------------------------------

_FENCED_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# Restricted to http(s) URLs with no whitespace/parens inside, so this never
# mis-fires on ordinary prose that happens to use square brackets (e.g. "the
# Foo[bar] method"); only a real markdown link immediately followed by a
# real-looking URL matches.
_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")
_BOLD_RE = re.compile(r"\*\*([^\n*][^\n]*?)\*\*")
_STRK_RE = re.compile(r"~~([^\n~]+?)~~")
_ITAL_STAR_RE = re.compile(r"(?<![*\w])\*([^\n*]+?)\*(?!\w)")
_ITAL_UNDERSCORE_RE = re.compile(r"(?<![_\w])_([^\n_]+?)_(?!\w)")
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_HRULE_RE = re.compile(r"^[ \t]*([-_*])\1{2,}[ \t]*$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_PIPE_TABLE_SEP_RE = re.compile(
    r"^\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$"
)


def _extract_fenced_code(text: str, stash) -> str:
    """Convert ```lang\\n...``` to Telegram's <pre><code class="language-X">.

    The language hint is what makes Telegram clients apply syntax
    highlighting; without it, a ```diff block renders as a single
    monochrome <pre> (the whole diff in one colour).
    With the language preserved, iOS/Android Telegram renders `+ added`
    in green and `- removed` in red. Desktop has fuller highlighting too.

    Falls back to plain <pre> when no language was specified, so prose
    triple-backtick blocks ("```text```") aren't affected.
    """
    def sub(m: "re.Match[str]") -> str:
        lang = m.group(1)
        body = m.group(2)
        if lang:
            return stash(
                f'<pre><code class="language-{lang}">'
                f"{html.escape(body)}</code></pre>"
            )
        return stash(f"<pre>{html.escape(body)}</pre>")
    return _FENCED_RE.sub(sub, text)


def _extract_inline_code(text: str, stash) -> str:
    def sub(m: "re.Match[str]") -> str:
        return stash(f"<code>{html.escape(m.group(1))}</code>")
    return _INLINE_CODE_RE.sub(sub, text)


def _extract_links(text: str, stash) -> str:
    """Convert [label](https://url) into Telegram's <a href> tag.

    Previously there was no markdown-link support: [label](url) fell
    through untouched, then got html.escape()'d into literal
    "[label](url)" visible text. Harmless for normal prose (nobody writes
    those by hand) but every WebSearch "Sources:" list is built entirely
    out of them, so search-backed replies rendered their citations as raw
    bracket/paren/URL text instead of clickable links. Both label and url are escaped
    here (not left for the later blanket html.escape(), which would also
    mangle the < / > this function itself emits) since the whole point
    of the stash mechanism is to protect already-finished HTML from that
    later pass.
    """
    def sub(m: "re.Match[str]") -> str:
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        return stash(f'<a href="{url}">{label}</a>')
    return _LINK_RE.sub(sub, text)


_QUOTE_LINE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?(.*)$")


def _convert_blockquotes(text: str) -> str:
    """Turn each run of "> " lines into one Telegram <blockquote>.

    The assistant formats drafts (emails, messages to send on) as markdown
    quotes. Previously there was no blockquote support, so every line
    arrived with a literal "> " on the front, which then had to be deleted
    by hand after copying. Telegram renders
    <blockquote> as a quote bar and copies the text without any marker.
    Only lines that START with ">" count, so "a > b" in prose is untouched.
    """
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        if _QUOTE_LINE_RE.match(lines[i]):
            body = []
            while i < len(lines):
                m = _QUOTE_LINE_RE.match(lines[i])
                if not m:
                    break
                body.append(m.group(1))
                i += 1
            while body and not body[-1].strip():
                body.pop()
            out.append(_QUOTE_OPEN + "\n".join(body) + _QUOTE_CLOSE)
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _convert_bold(text: str) -> str:
    return _BOLD_RE.sub(
        lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}", text)


def _convert_strikethrough(text: str) -> str:
    return _STRK_RE.sub(
        lambda m: f"{_STRK_OPEN}{m.group(1)}{_STRK_CLOSE}", text)


def _convert_italic(text: str) -> str:
    text = _ITAL_STAR_RE.sub(
        lambda m: f"{_ITAL_OPEN}{m.group(1)}{_ITAL_CLOSE}", text)
    text = _ITAL_UNDERSCORE_RE.sub(
        lambda m: f"{_ITAL_OPEN}{m.group(1)}{_ITAL_CLOSE}", text)
    return text


def _convert_bullets(text: str) -> str:
    # Telegram has no list rendering. Replace the markdown marker with U+2022
    # so the line at least reads as a bullet in any client.
    return _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)


def _convert_headings(text: str) -> str:
    # No heading tag in Telegram HTML; render bold.
    return _HEADING_RE.sub(
        lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}", text)


def _strip_hrules(text: str) -> str:
    text = _HRULE_RE.sub("", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip("\n")


# ---------------------------------------------------------------------------
# Inline math ($…$ / $$…$$) → best-effort Unicode rendering.
#
# Telegram has no math/LaTeX renderer. The closest thing we can do without
# shipping an image is to convert the common LaTeX vocabulary into Unicode
# so the result READS as math inline. `\alpha` → α, `x^2` → x², `\sum` → ∑.
# Anything we don't recognise is left untouched (the original \command stays
# visible) so the user can still tell what was intended.
#
# The conversion is intentionally limited to math regions (between `$` pairs)
# so prose dollar signs ("I have $40") aren't disturbed. The heuristic for a
# math region: the opening `$` must be immediately followed by a non-space
# character and the closing `$` must be immediately preceded by one, same
# convention LaTeX itself uses to disambiguate inline math from currency.
# `$$ ... $$` (display math) is also recognised and rendered inline.
# ---------------------------------------------------------------------------

# Unicode superscript / subscript translation tables. We use these for the
# common case x^2, x_n, k^{10} etc. Only chars present here are translated;
# anything else falls back to a literal "^(...)" / "_(...)" wrap so the math
# is at least legible.
_SUPERSCRIPT = str.maketrans(
    "0123456789+-=()abcdefghijklmnoprstuvwxyz",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ",
)
_SUBSCRIPT = str.maketrans(
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
)

# LaTeX command → single Unicode char. The order of replacement matters: we
# sort by length descending so e.g. `\leq` is matched before `\le`, and
# `\varepsilon` before `\var`. Anything not here passes through unchanged
# so the user can see what was intended.
_LATEX_SYMBOLS = {
    # Lowercase Greek
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "ϑ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π",
    r"\varpi": "ϖ", r"\rho": "ρ", r"\varrho": "ϱ", r"\sigma": "σ",
    r"\varsigma": "ς", r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ",
    r"\varphi": "ϕ", r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    # Uppercase Greek
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Upsilon": "Υ",
    r"\Phi": "Φ", r"\Psi": "Ψ", r"\Omega": "Ω",
    # Large operators
    r"\sum": "∑", r"\prod": "∏", r"\coprod": "∐", r"\int": "∫",
    r"\iint": "∬", r"\iiint": "∭", r"\oint": "∮", r"\bigcup": "⋃",
    r"\bigcap": "⋂", r"\bigvee": "⋁", r"\bigwedge": "⋀",
    # Binary ops
    r"\pm": "±", r"\mp": "∓", r"\times": "×", r"\div": "÷", r"\cdot": "·",
    r"\ast": "∗", r"\star": "⋆", r"\circ": "∘", r"\bullet": "•",
    r"\oplus": "⊕", r"\otimes": "⊗", r"\ominus": "⊖", r"\odot": "⊙",
    # Relations
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\neq": "≠", r"\ne": "≠", r"\equiv": "≡", r"\approx": "≈",
    r"\sim": "∼", r"\simeq": "≃", r"\cong": "≅", r"\propto": "∝",
    r"\subset": "⊂", r"\supset": "⊃", r"\subseteq": "⊆", r"\supseteq": "⊇",
    r"\in": "∈", r"\notin": "∉", r"\ni": "∋", r"\cup": "∪", r"\cap": "∩",
    r"\setminus": "∖", r"\perp": "⊥", r"\parallel": "∥", r"\mid": "∣",
    # Arrows
    r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←", r"\gets": "←",
    r"\Rightarrow": "⇒", r"\Leftarrow": "⇐", r"\leftrightarrow": "↔",
    r"\Leftrightarrow": "⇔", r"\iff": "⇔", r"\mapsto": "↦",
    r"\uparrow": "↑", r"\downarrow": "↓", r"\Uparrow": "⇑", r"\Downarrow": "⇓",
    # Logic / set theory
    r"\forall": "∀", r"\exists": "∃", r"\nexists": "∄", r"\neg": "¬",
    r"\lnot": "¬", r"\wedge": "∧", r"\land": "∧", r"\vee": "∨", r"\lor": "∨",
    r"\emptyset": "∅", r"\varnothing": "∅", r"\therefore": "∴",
    r"\because": "∵",
    # Misc
    r"\partial": "∂", r"\nabla": "∇", r"\infty": "∞", r"\hbar": "ℏ",
    r"\ell": "ℓ", r"\Re": "ℜ", r"\Im": "ℑ", r"\aleph": "ℵ",
    r"\angle": "∠", r"\triangle": "△", r"\square": "□",
    r"\dots": "…", r"\ldots": "…", r"\cdots": "⋯", r"\vdots": "⋮",
    r"\ddots": "⋱", r"\prime": "′",
    # Blackboard bold sets (the curly form arrives via _GENERIC_BB_RE below).
    r"\mathbb{R}": "ℝ", r"\mathbb{N}": "ℕ", r"\mathbb{Z}": "ℤ",
    r"\mathbb{Q}": "ℚ", r"\mathbb{C}": "ℂ", r"\mathbb{P}": "ℙ",
    # Spacing and braces: drop them; they add nothing in plain Unicode.
    r"\,": "", r"\;": "", r"\:": "", r"\!": "", r"\quad": " ",
    r"\qquad": "  ", r"\left": "", r"\right": "",
}

# Sorted longest-first so \leq beats \le, \mathbb{R} beats \mathbb, etc.
_LATEX_SYMBOL_PAIRS = sorted(
    _LATEX_SYMBOLS.items(), key=lambda kv: -len(kv[0])
)

# Match inline math: $...$ (no whitespace immediately inside) OR $$...$$.
# The negative-whitespace assertion is what excludes "$40 and $50": prose
# dollar signs have a space after them; math doesn't.
_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$(\S(?:[^$\n]*\S)?)\$(?!\$)")

# \frac{a}{b} → a/b ; \sqrt{x} → √x ; \mathbf{x} → 𝐱 (kept simple as plain x);
# \text{words} → words. Applied AFTER symbol replacement so any nested LaTeX
# inside the braces has already been resolved.
_FRAC_RE = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_BRACE_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_BARE_COMMAND_BRACE_RE = re.compile(
    r"\\(?:text|mathrm|mathbf|mathit|mathsf|mathtt|operatorname)\s*\{([^{}]*)\}"
)
# \mathbb{X} where X isn't already in the symbol table → leave as X.
_GENERIC_BB_RE = re.compile(r"\\mathbb\s*\{([^{}]*)\}")

# Superscript / subscript. Brace form captures multi-char content; bare form
# only grabs one following char (LaTeX convention).
_SUP_BRACE_RE = re.compile(r"\^\s*\{([^{}]*)\}")
_SUP_BARE_RE = re.compile(r"\^(\S)")
_SUB_BRACE_RE = re.compile(r"_\s*\{([^{}]*)\}")
_SUB_BARE_RE = re.compile(r"_(\S)")


def _translate_or_wrap(s: str, table: dict, fallback_prefix: str) -> str:
    """Try to render `s` using the given super/sub table. If every char has a
    mapping, return the translated form. Otherwise wrap as a textual fallback
    so the math is still legible (e.g. x^{abc} → x^(abc) when no superscript
    glyphs exist for those letters)."""
    translated = s.translate(table)
    # If translation produced ASCII-only output for an all-ASCII input, no
    # mapping happened; show a textual wrapper instead.
    if translated == s and any(c not in " " for c in s):
        return f"{fallback_prefix}({s})"
    return translated


def _render_math(text: str) -> str:
    """Convert one math region's content (no surrounding $) to Unicode."""
    # Order matters: replace named commands first so leftover content can
    # then have ^/_ patterns applied to (now-Unicode) characters.
    for cmd, glyph in _LATEX_SYMBOL_PAIRS:
        text = text.replace(cmd, glyph)
    text = _FRAC_RE.sub(lambda m: f"({m.group(1)})/({m.group(2)})", text)
    text = _SQRT_BRACE_RE.sub(lambda m: f"√({m.group(1)})", text)
    text = _BARE_COMMAND_BRACE_RE.sub(lambda m: m.group(1), text)
    text = _GENERIC_BB_RE.sub(lambda m: m.group(1), text)
    # Super/sub: brace form first (greedy multi-char), then bare single char.
    text = _SUP_BRACE_RE.sub(
        lambda m: _translate_or_wrap(m.group(1), _SUPERSCRIPT, "^"), text)
    text = _SUP_BARE_RE.sub(
        lambda m: _translate_or_wrap(m.group(1), _SUPERSCRIPT, "^"), text)
    text = _SUB_BRACE_RE.sub(
        lambda m: _translate_or_wrap(m.group(1), _SUBSCRIPT, "_"), text)
    text = _SUB_BARE_RE.sub(
        lambda m: _translate_or_wrap(m.group(1), _SUBSCRIPT, "_"), text)
    return text


def _convert_math(text: str) -> str:
    """Find inline ($…$) and display ($$…$$) math regions and Unicode-ify them.

    Display math is converted in place; we don't shift it onto its own line
    or wrap it in `<pre>`, because Telegram's mobile clients break long pre
    blocks awkwardly. The result reads as inline prose with Unicode math.
    """
    # Display math first so $$ doesn't get eaten by the single-$ pattern.
    text = _DISPLAY_MATH_RE.sub(lambda m: _render_math(m.group(1)), text)
    text = _INLINE_MATH_RE.sub(lambda m: _render_math(m.group(1)), text)
    return text


# Tables NEVER render as <pre> monospace. Two field-tested reasons:
# wider-than-bubble grids wrap into interleaved fragments (no horizontal
# scroll on phones), and Telegram draws monospace in a noticeably SMALLER
# font than body text, which reads poorly. Every pipe table reflows into proportional-font lines via
# _render_table_as_lines instead.

# Emphasis markers are stripped from cells we re-wrap in our own ** **
# (nested bold breaks the parser).
_EMPHASIS_MARKS_RE = re.compile(r"\*\*|__|`")


def _strip_emphasis(cell: str) -> str:
    return _EMPHASIS_MARKS_RE.sub("", cell)


def _convert_pipe_tables(text: str, stash) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if "|" in stripped and i + 1 < n and _PIPE_TABLE_SEP_RE.match(lines[i + 1].strip()):
            header_cells = _split_pipe_row(stripped)
            i += 2  # consume header + separator
            rows = [header_cells]
            while i < n:
                cand = lines[i].strip()
                if not cand or "|" not in cand:
                    break
                rows.append(_split_pipe_row(cand))
                i += 1
            # Reflow into list/card lines. These are emitted as markdown
            # and flow through the rest of the pipeline, so ** ** inside
            # cells becomes real <b> here, and everything renders at body
            # text size (never the smaller <pre> monospace).
            rendered = _render_table_as_lines(rows)
            if not rendered:
                # Degenerate (e.g. zero-cell rows). Fall back to original line.
                out.append(line)
                continue
            out.append(rendered)
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _split_pipe_row(row: str) -> list[str]:
    body = row
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _render_table_as_lines(rows: list[list[str]]) -> str:
    """Reflow a pipe table into proportional-font lines.

    Two-column tables become a key-value list (the shape that reads best
    on phones):
        • **key:** value
    Wider tables become one line per row, first non-empty cell as the bold
    lead and the remaining cells labelled by their headers:
        **lead** · Price: $4.50 · Qty: 10
    Header labels and lead cells are emphasis-stripped because we wrap them
    in our own ** **; value cells keep their markdown and convert normally.
    """
    if not rows or max(len(r) for r in rows) == 0:
        return ""
    header, data = rows[0], rows[1:]
    if not data:
        title = " · ".join(_strip_emphasis(c) for c in header if c.strip())
        return f"**{title}**" if title else ""
    ncols = max(len(r) for r in rows)

    if ncols == 2:
        lines = []
        for r in data:
            k = _strip_emphasis(r[0]).rstrip(":") if r else ""
            v = r[1] if len(r) > 1 else ""
            if k:
                lines.append(f"• **{k}:** {v}".rstrip())
            elif v:
                lines.append(f"• {v}")
        return "\n".join(lines)

    lines = []
    for r in data:
        lead_idx = next((ci for ci, c in enumerate(r) if c.strip()), None)
        if lead_idx is None:
            continue
        parts = [f"**{_strip_emphasis(r[lead_idx])}**"]
        for ci in range(ncols):
            if ci == lead_idx:
                continue
            val = r[ci] if ci < len(r) else ""
            if not val.strip():
                continue
            label = header[ci].strip() if ci < len(header) else ""
            label = _strip_emphasis(label).rstrip(":")
            parts.append(f"{label}: {val}" if label else val)
        lines.append(" · ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-tests. Run `python3 scripts/md_telegram.py` to execute them.
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    cases: list[tuple[str, str, str]] = [
        # (label, input, expected_output)

        ("plain ascii passthrough",
         "hello world",
         "hello world"),

        ("blockquote run becomes one <blockquote> without > markers",
         "Draft:\n\n> Hi Alice,\n>\n> Thanks for the **report**.\n> - one\n\nDone",
         "Draft:\n\n<blockquote>Hi Alice,\n\nThanks for the <b>report</b>.\n• one</blockquote>\n\nDone"),

        ("> mid-line is not a quote",
         "if a > b then",
         "if a &gt; b then"),

        ("html-escapes ampersand and angle brackets",
         "a & b < c > d",
         "a &amp; b &lt; c &gt; d"),

        ("bold",
         "this is **bold** here",
         "this is <b>bold</b> here"),

        ("bold with spaces and punctuation",
         "**Acme Store** (Example Shop)",
         "<b>Acme Store</b> (Example Shop)"),

        ("italic with asterisks",
         "this is *italic* here",
         "this is <i>italic</i> here"),

        ("italic with underscores",
         "this is _italic_ here",
         "this is <i>italic</i> here"),

        ("inline code escapes content",
         "run `rm -rf /<x>`",
         "run <code>rm -rf /&lt;x&gt;</code>"),

        ("fenced code block",
         "before\n```\nrm -rf /\n```\nafter",
         "before\n<pre>rm -rf /\n</pre>\nafter"),

        ("fenced code block with language tag",
         "```python\nprint('hi')\n```",
         "<pre><code class=\"language-python\">print(&#x27;hi&#x27;)\n</code></pre>"),

        ("fenced diff block gets diff syntax class",
         "```diff\n- old\n+ new\n```",
         "<pre><code class=\"language-diff\">- old\n+ new\n</code></pre>"),

        ("inline code does not get markdown applied",
         "`**not bold**`",
         "<code>**not bold**</code>"),

        ("heading becomes bold",
         "# Title\nbody",
         "<b>Title</b>\nbody"),

        ("bullet dash becomes •",
         "- first\n- second",
         "• first\n• second"),

        ("bullet asterisk becomes •",
         "* first\n* second",
         "• first\n• second"),

        ("horizontal rule is removed",
         "above\n\n---\n\nbelow",
         "above\n\nbelow"),

        ("strikethrough",
         "old ~~price~~",
         "old <s>price</s>"),

        ("markdown link becomes a clickable <a> tag",
         "[Anthropic](https://www.anthropic.com/)",
         '<a href="https://www.anthropic.com/">Anthropic</a>'),

        ("sources list: every link converts, not just the first",
         "Sources:\n- [First Title](https://example.com/one)\n"
         "- [Second Title](https://example.com/two)\n"
         "- [Third Title](https://example.com/three)",
         "Sources:\n"
         '• <a href="https://example.com/one">First Title</a>\n'
         '• <a href="https://example.com/two">Second Title</a>\n'
         '• <a href="https://example.com/three">Third Title</a>'),

        ("link label and url are html-escaped",
         "[A & B](https://example.com/?x=1&y=2)",
         '<a href="https://example.com/?x=1&amp;y=2">A &amp; B</a>'),

        ("bracketed text without a real url is left alone",
         "the Foo[bar] method (not a link)",
         "the Foo[bar] method (not a link)"),

        ("partial bold left as literal",
         "Hello **world (no close)",
         "Hello **world (no close)"),

        ("partial fenced ignored",
         "Hello ``` no close",
         "Hello ``` no close"),

        ("dollar signs pass through unescaped",
         "$40 minimum",
         "$40 minimum"),

        ("bold with dollar sign and slash",
         "**$37.60/unit**",
         "<b>$37.60/unit</b>"),
    ]

    pipe_table_in = (
        "before\n\n"
        "| Product | Price | Qty |\n"
        "|---|---|---|\n"
        "| Widget | $4.50 | 10 |\n"
        "| Gadget | $4.50 | 20 |\n"
        "\nafter"
    )
    pipe_table_out_expected = (
        "before\n\n"
        "<b>Widget</b> · Price: $4.50 · Qty: 10\n"
        "<b>Gadget</b> · Price: $4.50 · Qty: 20\n"
        "\nafter"
    )

    bold_in_table_in = (
        "| **A** | B |\n"
        "|---|---|\n"
        "| **x** | y |\n"
    )
    # 2-col tables become a key-value list; the key's own emphasis markers
    # are stripped because we wrap it in our own bold.
    bold_in_table_out = "• <b>x:</b> y"

    # 2-col tables reflow into a key-value list in proportional font.
    wide_two_col_in = (
        "| Setting | Value |\n"
        "|---|---|\n"
        "| Draft streaming interval for DMs | 0.25 seconds between frames |\n"
        "| Edit interval for groups | 0.6 seconds between message edits |\n"
    )
    wide_two_col_out = (
        "• <b>Draft streaming interval for DMs:</b> 0.25 seconds between frames\n"
        "• <b>Edit interval for groups:</b> 0.6 seconds between message edits"
    )

    # 3+ column tables become one line per row: bold lead cell, then
    # header-labelled fields. Markdown inside cells converts for real now.
    wide_three_col_in = (
        "| Product | Price | Notes |\n"
        "|---|---|---|\n"
        "| **Widget** | $4.50 | Blue, ships in a padded envelope |\n"
        "| Gadget | $6.00 | Example notes, currently out of stock |\n"
    )
    wide_three_col_out = (
        "<b>Widget</b> · Price: $4.50 · Notes: Blue, ships in a padded envelope\n"
        "<b>Gadget</b> · Price: $6.00 · Notes: Example notes, currently out of stock"
    )

    full_mixed_in = (
        "**Acme Store** (Example Shop)\n"
        "- **Example Shop** sells widgets at **$37.60/unit**, 2 left in stock\n"
        "- Two \"Add to Cart\" buttons visible\n"
        "\n---\n\n"
        "**Gadget Co / Parts**\n"
        "- Spare Parts: Active, $100/box, but 0 rows now\n"
    )

    failures = 0

    for label, src, expected in cases:
        got = md_to_telegram_html(src)
        if got != expected:
            failures += 1
            print(f"FAIL  {label}")
            print(f"  input:    {src!r}")
            print(f"  expected: {expected!r}")
            print(f"  got:      {got!r}")
        else:
            print(f"ok    {label}")

    got = md_to_telegram_html(pipe_table_in)
    if got != pipe_table_out_expected:
        failures += 1
        print("FAIL  pipe table 3-col reflows to labelled row lines")
        print(f"  expected: {pipe_table_out_expected!r}")
        print(f"  got:      {got!r}")
    else:
        print("ok    pipe table 3-col reflows to labelled row lines")

    got = md_to_telegram_html(bold_in_table_in)
    if got != bold_in_table_out:
        failures += 1
        print("FAIL  2-col table reflows, key emphasis stripped")
        print(f"  expected: {bold_in_table_out!r}")
        print(f"  got:      {got!r}")
    else:
        print("ok    2-col table reflows, key emphasis stripped")

    got = md_to_telegram_html(wide_two_col_in)
    if got != wide_two_col_out:
        failures += 1
        print("FAIL  wide 2-col table reflows to key-value list")
        print(f"  expected: {wide_two_col_out!r}")
        print(f"  got:      {got!r}")
    else:
        print("ok    wide 2-col table reflows to key-value list")

    got = md_to_telegram_html(wide_three_col_in)
    if got != wide_three_col_out:
        failures += 1
        print("FAIL  wide 3-col table reflows to labelled row lines")
        print(f"  expected: {wide_three_col_out!r}")
        print(f"  got:      {got!r}")
    else:
        print("ok    wide 3-col table reflows to labelled row lines")

    # No table may ever reach <pre>: Telegram renders monospace in a
    # smaller font and wraps wide grids into fragments.
    for src in (pipe_table_in, bold_in_table_in, wide_three_col_in):
        got = md_to_telegram_html(src)
        if "<pre>" in got:
            failures += 1
            print(f"FAIL  table leaked into <pre>: {got!r}")
            break
    else:
        print("ok    no table renders as <pre>")

    got = md_to_telegram_html(full_mixed_in)
    must_contain = [
        "<b>Acme Store</b>",
        "• <b>Example Shop</b> sells widgets at <b>$37.60/unit</b>",
        "<b>Gadget Co / Parts</b>",
        "Two &quot;Add to Cart&quot; buttons",
    ]
    must_not_contain = ["---", "**"]
    bad = [s for s in must_contain if s not in got]
    bad += [s for s in must_not_contain if s in got]
    if bad:
        failures += 1
        print("FAIL  full mixed conversion")
        print(f"  problems with: {bad}")
        print(f"  got:           {got!r}")
    else:
        print("ok    full mixed conversion")

    long_src = "a" * 4500
    got = md_to_telegram_html(long_src)
    if not (got.endswith("...(truncated)") and len(got) < 4100):
        failures += 1
        print(f"FAIL  truncation: len={len(got)} tail={got[-40:]!r}")
    else:
        print("ok    truncation")

    # ── math conversion ────────────────────────────────────────────────────
    math_cases = [
        # (input, must_contain_each, must_not_contain_any, label)
        ("$x^2 + y^2 = z^2$", ["x²", "y²", "z²"], ["$", "^"], "inline superscript"),
        ("$x_i$", ["xᵢ"], ["$", "_"], "inline subscript"),
        ("$\\alpha + \\beta$", ["α", "β"], ["\\alpha", "\\beta"], "Greek letters"),
        ("$\\sum_{i=1}^n i$", ["∑"], ["\\sum"], "sum operator"),
        ("$\\frac{a}{b}$", ["(a)/(b)"], ["\\frac"], "fraction"),
        ("$\\sqrt{2}$", ["√(2)"], ["\\sqrt"], "square root"),
        ("$\\pi \\approx 3.14$", ["π", "≈"], ["\\pi", "\\approx"], "constants and relations"),
        # Currency must NOT be touched.
        ("I have $40 and $50 in my wallet", ["$40", "$50"], [], "currency untouched"),
        # Display math.
        ("$$E = mc^2$$", ["E", "mc²"], ["$$"], "display math"),
        # Math inside backtick code must NOT be converted.
        ("Use `$x^2$` for math", ["$x^2$"], ["x²"], "code-block math untouched"),
    ]
    for src, must_have, must_not_have, label in math_cases:
        got = md_to_telegram_html(src)
        missing = [s for s in must_have if s not in got]
        leaked = [s for s in must_not_have if s in got]
        if missing or leaked:
            failures += 1
            print(f"FAIL  math: {label}")
            print(f"  src:     {src!r}")
            print(f"  got:     {got!r}")
            if missing: print(f"  missing: {missing}")
            if leaked:  print(f"  leaked:  {leaked}")
        else:
            print(f"ok    math: {label}")

    print()
    if failures:
        print(f"{failures} failure(s)")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(_run_self_tests())
