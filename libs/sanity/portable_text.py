"""Convert Sanity Portable Text to Markdown (stdlib only).

Portable Text is a list of block dicts. The dlthub blog body uses six block
types, all rendered here:

- ``block`` — paragraphs/headings/lists with ``style`` (``h1``-``h6``,
  ``blockquote``, ``normal``) and optional ``listItem`` (``bullet``/``number``)
  + ``level`` for nesting. Spans carry ``marks`` (``strong``, ``em``, ``code``)
  and ``markDefs`` links.
- ``image`` — rendered as ``![alt](url)`` from the GROQ-resolved ``url``.
- ``code`` — rendered as a fenced code block with its ``language``.
- ``markdownBlock`` — already-authored Markdown, emitted verbatim.
- ``iframe`` — raw HTML embed, emitted verbatim (Markdown allows inline HTML).
- ``youtube`` — rendered as a link to the video ``url``.

Any *other* block type is surfaced as an HTML comment marker rather than
dropped silently, so unexpected CMS content is visible in the archive instead of
disappearing.
"""

from __future__ import annotations

import re
from typing import Any

_HEADING_STYLES = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Backslash-escape the inline-significant characters so literal prose (e.g. a
# stray ``*`` or ``[``) survives a round-trip instead of being reinterpreted as
# Markdown syntax. ``str.translate`` visits each char once, so the leading
# backslash mapping never double-escapes.
_MD_ESCAPE = {ord(c): f"\\{c}" for c in r"\`*_[]()"}
# HTML-sensitive characters become entities so literal ``<tag>`` / ``&`` in body
# text renders as text instead of being parsed as HTML (a fidelity gap and a
# stray-markup vector). Entities, not backslash escapes — not every Markdown
# renderer honors ``\<``. ``translate`` does not re-scan replacements, so
# ``&`` -> ``&amp;`` never double-encodes. Verbatim paths (``markdownBlock``,
# ``iframe``, fenced/inline ``code``) deliberately skip this.
_MD_ESCAPE.update({ord("&"): "&amp;", ord("<"): "&lt;", ord(">"): "&gt;"})

# Block-leading syntax (``#`` heading, ``>`` quote, ``-``/``+`` bullet) that the
# inline escaper above does not cover — it only bites at the start of a line.
_LEADING_SYMBOL_RE = re.compile(r"^(\s*)(#{1,6}|[>+-])(\s|$)")
# Ordered-list markers (``1.`` / ``1)``) need the backslash before the delimiter.
_LEADING_ORDERED_RE = re.compile(r"^(\s*)(\d+)([.)])(\s|$)")
# A line that is only ``-`` or ``=`` runs (with optional spaces) renders as a
# thematic break / setext-heading underline. ``*`` and ``_`` runs are already
# neutralized by the inline escaper, so only ``-``/``=`` reach here; backslash
# the first symbol so the line stays literal text.
_THEMATIC_BREAK_RE = re.compile(r"^(\s*)([-=])([-=\s]*)$")
# A code-fence info string must be a single token (no whitespace/backticks) or
# it can break the fence; languages like ``c++``, ``c#``, ``f#``, ``.net`` fit.
_LANG_TOKEN_RE = re.compile(r"[A-Za-z0-9_+#.-]+")
# A trailing run of ``#`` (optionally followed by spaces) is parsed as an ATX
# heading-close sequence and stripped; backslash it to keep the hash literal.
_TRAILING_ATX_RE = re.compile(r"#+(?=\s*$)")


def escape_trailing_atx(text: str) -> str:
    """Neutralize a trailing ATX heading-close (``...#``) so it isn't stripped.

    Public so the blog downloader's title heading and this module's heading
    blocks share one rule.
    """
    return _TRAILING_ATX_RE.sub(lambda m: "\\" + m.group(0), text)


def _escape_md(text: str) -> str:
    return text.translate(_MD_ESCAPE)


def escape_text(text: str) -> str:
    """Escape inline Markdown/HTML metacharacters in CMS-sourced plain text.

    Public helper for callers that emit text *outside* a Portable Text block
    (e.g. the blog downloader's title heading) but still need it neutralized the
    same way span text is, so a title with ``#``/``<``/``[`` can't change the
    rendered structure.
    """
    return _escape_md(text)


def _inline_code(text: str) -> str:
    """Wrap ``text`` as an inline code span, surviving embedded backticks.

    Per CommonMark, a code span uses a backtick run longer than any run inside
    it; when the content touches a backtick the span is padded with single
    spaces (which the renderer strips) so the delimiters stay distinct. The same
    padding preserves content that *both* begins and ends with a space — which
    CommonMark would otherwise strip one space from each side of — unless the
    content is all spaces (which is left intact).
    """
    longest_run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest_run + 1)
    both_edge_space = len(text) >= 2 and text[0] == " " and text[-1] == " "
    needs_padding = longest_run > 0 or (both_edge_space and text.strip() != "")
    if needs_padding:
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def _escape_block_leading(text: str) -> str:
    """Neutralize Markdown block syntax at the start of *every* line.

    Span text can contain hard line breaks, so a single anchored pass would only
    protect the first line and let ``foo\\n# bar`` render a real heading on the
    second. Each line is escaped independently, then rejoined.
    """
    escaped_lines: list[str] = []
    for line in text.split("\n"):
        line = _LEADING_SYMBOL_RE.sub(r"\1\\\2\3", line)
        line = _LEADING_ORDERED_RE.sub(r"\1\2\\\3\4", line)
        line = _THEMATIC_BREAK_RE.sub(r"\1\\\2\3", line)
        escaped_lines.append(line)
    return "\n".join(escaped_lines)


def _format_link_dest(href: str) -> str:
    """Render a link destination that survives Markdown parsing.

    A bare ``(...)`` destination breaks when the URL contains spaces or
    parentheses, so any such href is wrapped in the angle-bracket form
    (``<...>``) with the few characters illegal inside it escaped.
    """
    if any(ch in href for ch in " ()<>"):
        inner = href.replace("\\", "\\\\").replace("<", "\\<").replace(">", "\\>")
        return f"<{inner}>"
    return href


def _render_spans(block: dict[str, Any]) -> str:
    """Render a block's child spans, applying marks and link definitions."""
    children = block.get("children") or []
    mark_defs = {md["_key"]: md for md in (block.get("markDefs") or []) if "_key" in md}

    parts: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        raw_text = child.get("text", "")
        marks = child.get("marks") or []

        decorators = [m for m in marks if m in {"strong", "em", "code", "underline"}]
        link_keys = [m for m in marks if m in mark_defs]

        # Code spans are verbatim — Markdown does not interpret syntax inside
        # backticks — so they skip escaping; everything else is escaped.
        if "code" in decorators:
            text = _inline_code(raw_text)
        else:
            text = _escape_md(raw_text)
            # A raw ``\n`` renders as a soft wrap; emit an explicit hard break so
            # intentional line breaks in the source survive in the archive.
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            text = text.replace("\n", "  \n")

        if "strong" in decorators:
            text = f"**{text}**"
        if "em" in decorators:
            text = f"_{text}_"
        if "underline" in decorators:
            # Markdown has no underline; pass through HTML, which renderers honor.
            text = f"<u>{text}</u>"

        for key in link_keys:
            mark = mark_defs[key]
            if mark.get("_type") == "link" and mark.get("href"):
                text = f"[{text}]({_format_link_dest(mark['href'])})"

        parts.append(text)

    return "".join(parts)


def _prefix_lines(text: str, first: str, rest: str) -> str:
    """Prefix the first line with ``first`` and every continuation with ``rest``.

    Span text can carry hard line breaks, so a blockquote/list wrapper applied
    only to the first line would drop continuation lines out of the quote/list.
    """
    lines = text.split("\n")
    out = [f"{first}{lines[0]}"]
    out += [f"{rest}{line}" for line in lines[1:]]
    return "\n".join(out)


def _render_block(block: dict[str, Any]) -> str | None:
    style = block.get("style", "normal")
    text = _render_spans(block)

    list_item = block.get("listItem")
    if list_item:
        level = max(int(block.get("level", 1) or 1), 1)
        indent = "  " * (level - 1)
        bullet = "1." if list_item == "number" else "-"
        # Continuation lines align under the content (past the bullet + space) so
        # multiline list items stay inside the item instead of breaking out.
        cont = indent + " " * (len(bullet) + 1)
        return _prefix_lines(_escape_block_leading(text), f"{indent}{bullet} ", cont)

    if style in _HEADING_STYLES:
        # A heading is one line: collapse any hard breaks the span renderer
        # introduced, and neutralize a trailing ``#`` so ATX-close parsing can't
        # strip it.
        heading = escape_trailing_atx(text.replace("  \n", " ").replace("\n", " "))
        return f"{'#' * int(style[1])} {heading}"
    if style == "blockquote":
        # Prefix every line so a multiline quote stays a single blockquote.
        return _prefix_lines(_escape_block_leading(text), "> ", "> ")
    return _escape_block_leading(text)


def _render_image(block: dict[str, Any]) -> str | None:
    url = block.get("url")
    if not url:
        return None
    alt = _escape_md(block.get("alt") or "")
    return f"![{alt}]({_format_link_dest(url)})"


def _render_code(block: dict[str, Any]) -> str | None:
    # Distinguish an absent ``code`` (skip the block) from an empty string (a
    # blank code block worth preserving as an empty fence).
    code = block.get("code")
    if code is None:
        return None
    # The language goes into the fence's info string; whitespace, backticks, or
    # newlines there would break the fence open. Keep it only if it's a single
    # safe token, otherwise drop it (an unlabeled fence still renders fine).
    language = block.get("language") or ""
    if not _LANG_TOKEN_RE.fullmatch(language):
        language = ""
    # Use a fence long enough to survive backticks inside the snippet.
    longest_run = max((len(m) for m in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{code}\n{fence}"


def _render_iframe(block: dict[str, Any]) -> str | None:
    # The CMS stores the full <iframe ...> markup under ``code``; Markdown
    # passes raw HTML through, so emit it verbatim. ``None`` (absent) is skipped;
    # an empty string is preserved rather than silently dropped.
    return block.get("code")


def _render_youtube(block: dict[str, Any]) -> str | None:
    url = block.get("url")
    if not url:
        return None
    return f"[Watch on YouTube]({_format_link_dest(url)})"


def _render_markdown_block(block: dict[str, Any]) -> str | None:
    # Already-authored Markdown — emit as-is so tables/HTML survive intact.
    # ``None`` (absent) is skipped; an empty string is preserved as a spacer.
    return block.get("markdown")


def _comment_safe(text: str) -> str:
    """Neutralize sequences that could break out of an HTML comment.

    The unsupported-block marker embeds a block ``_type``; a value containing
    ``>`` or ``-->`` could otherwise close the comment early and inject markup
    into the archived Markdown. Drop angle brackets and collapse ``--`` runs so
    the value stays inside the comment.
    """
    safe = text.replace("<", "").replace(">", "")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe


def to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Render a Portable Text body to a Markdown string."""
    renderers = {
        "block": _render_block,
        "image": _render_image,
        "code": _render_code,
        "iframe": _render_iframe,
        "youtube": _render_youtube,
        "markdownBlock": _render_markdown_block,
    }

    lines: list[str] = []
    for block in blocks or []:
        block_type = block.get("_type")
        renderer = renderers.get(block_type) if block_type else None
        if renderer is not None:
            rendered = renderer(block)
        else:
            # Surface, don't drop: an unexpected block type stays visible in the
            # archive source as a comment instead of vanishing.
            rendered = (
                f"<!-- unsupported Portable Text block: "
                f"{_comment_safe(block_type or '')} -->"
            )
        if rendered is not None:
            lines.append(rendered)

    # Join with our own separator only; do not strip, so a verbatim block's
    # significant leading/trailing whitespace (e.g. a markdownBlock) is kept
    # byte-for-byte.
    return "\n\n".join(lines)
