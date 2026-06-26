"""Tests for the Portable Text -> Markdown converter."""

from typing import Any

from libs.sanity.portable_text import to_markdown


def _block(style: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "_type": "block",
        "style": style,
        "children": [{"_type": "span", "marks": [], "text": text}],
        "markDefs": [],
        **extra,
    }


def test_headings_and_paragraph():
    blocks = [
        _block("h2", "Summary"),
        _block("normal", "Hello world."),
    ]
    assert to_markdown(blocks) == "## Summary\n\nHello world."


def test_marks_strong_em_code():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [],
        "children": [
            {"_type": "span", "marks": ["strong"], "text": "bold"},
            {"_type": "span", "marks": [], "text": " and "},
            {"_type": "span", "marks": ["em"], "text": "italic"},
            {"_type": "span", "marks": ["code"], "text": "code"},
        ],
    }
    assert to_markdown([block]) == "**bold** and _italic_`code`"


def test_link_mark_def():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [{"_key": "abc", "_type": "link", "href": "https://dlthub.com"}],
        "children": [{"_type": "span", "marks": ["abc"], "text": "dltHub"}],
    }
    assert to_markdown([block]) == "[dltHub](https://dlthub.com)"


def test_bullet_and_numbered_lists_with_nesting():
    blocks = [
        _block("normal", "Item A", listItem="bullet", level=1),
        _block("normal", "Nested", listItem="bullet", level=2),
        _block("normal", "Step 1", listItem="number", level=1),
    ]
    assert to_markdown(blocks) == "- Item A\n\n  - Nested\n\n1. Step 1"


def test_blockquote():
    assert to_markdown([_block("blockquote", "quoted")]) == "> quoted"


def test_image_block_uses_resolved_url():
    blocks = [
        {
            "_type": "image",
            "url": "https://cdn.sanity.io/images/x/y/z.png",
            "alt": "diagram",
        },
    ]
    assert to_markdown(blocks) == "![diagram](https://cdn.sanity.io/images/x/y/z.png)"


def test_image_without_url_is_skipped():
    assert to_markdown([{"_type": "image"}]) == ""


def test_unknown_block_type_is_surfaced_as_comment():
    blocks = [{"_type": "mysteryWidget", "foo": "bar"}, _block("normal", "kept")]
    assert to_markdown(blocks) == (
        "<!-- unsupported Portable Text block: mysteryWidget -->\n\nkept"
    )


def test_empty_body():
    assert to_markdown([]) == ""


def test_reserved_chars_are_escaped():
    block = _block("normal", "a*b_c[d](e) f\\g")
    assert to_markdown([block]) == r"a\*b\_c\[d\]\(e\) f\\g"


def test_code_span_is_not_escaped():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [],
        "children": [{"_type": "span", "marks": ["code"], "text": "a*b_c"}],
    }
    assert to_markdown([block]) == "`a*b_c`"


def test_inline_code_with_backtick_uses_longer_fence():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [],
        "children": [{"_type": "span", "marks": ["code"], "text": "a`b"}],
    }
    assert to_markdown([block]) == "`` a`b ``"


def test_image_alt_text_is_escaped():
    blocks = [{"_type": "image", "url": "https://x.io/z.png", "alt": "a]b[c"}]
    assert to_markdown(blocks) == r"![a\]b\[c](https://x.io/z.png)"


def test_underline_mark_rendered_as_html():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [],
        "children": [{"_type": "span", "marks": ["underline"], "text": "under"}],
    }
    assert to_markdown([block]) == "<u>under</u>"


def test_link_text_escaped_and_href_with_parens_angle_bracketed():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [{"_key": "k", "_type": "link", "href": "https://x.io/(a)_b"}],
        "children": [{"_type": "span", "marks": ["k"], "text": "a*b"}],
    }
    assert to_markdown([block]) == r"[a\*b](<https://x.io/(a)_b>)"


def test_simple_href_left_bare():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [{"_key": "k", "_type": "link", "href": "https://dlthub.com/x"}],
        "children": [{"_type": "span", "marks": ["k"], "text": "link"}],
    }
    assert to_markdown([block]) == "[link](https://dlthub.com/x)"


def test_leading_block_syntax_is_escaped():
    blocks = [
        _block("normal", "# not a heading"),
        _block("normal", "> not a quote"),
        _block("normal", "- not a bullet"),
        _block("normal", "1. not a list"),
    ]
    # ``>`` is neutralized as an HTML entity by the inline escaper (rendering as
    # a literal ``>``, not a blockquote), so it never reaches the block-leading
    # backslash escaper; ``#``/``-``/``1.`` still get the backslash treatment.
    assert to_markdown(blocks) == (
        "\\# not a heading\n\n&gt; not a quote\n\n\\- not a bullet\n\n1\\. not a list"
    )


def test_list_item_leading_syntax_is_escaped():
    blocks = [_block("normal", "# nope", listItem="bullet", level=1)]
    assert to_markdown(blocks) == "- \\# nope"


def test_code_block_fenced_with_language():
    blocks = [{"_type": "code", "code": "print('hi')", "language": "python"}]
    assert to_markdown(blocks) == "```python\nprint('hi')\n```"


def test_code_block_fence_grows_past_inner_backticks():
    blocks = [{"_type": "code", "code": "a ``` b", "language": ""}]
    assert to_markdown(blocks) == "````\na ``` b\n````"


def test_markdown_block_emitted_verbatim():
    md = "| a | b |\n|---|---|\n| 1 | 2 |"
    assert to_markdown([{"_type": "markdownBlock", "markdown": md}]) == md


def test_iframe_emitted_verbatim():
    html = '<iframe src="https://embed.example/x"></iframe>'
    assert to_markdown([{"_type": "iframe", "code": html}]) == html


def test_youtube_rendered_as_link():
    blocks = [{"_type": "youtube", "url": "https://youtube.com/watch?v=abc"}]
    assert to_markdown(blocks) == "[Watch on YouTube](https://youtube.com/watch?v=abc)"


def test_html_sensitive_chars_escaped_in_text():
    # Literal <, >, & in prose must become entities so a Markdown renderer
    # treats them as text, not as HTML tags.
    blocks = [_block("normal", "use a < b && c > d in <script>code</script>")]
    assert to_markdown(blocks) == (
        "use a &lt; b &amp;&amp; c &gt; d in &lt;script&gt;code&lt;/script&gt;"
    )


def test_html_chars_escaped_in_heading_and_alt():
    assert to_markdown([_block("h2", "A & B <ok>")]) == "## A &amp; B &lt;ok&gt;"
    img = [{"_type": "image", "url": "https://x/i.png", "alt": "a & <b>"}]
    assert to_markdown(img) == "![a &amp; &lt;b&gt;](https://x/i.png)"


def test_verbatim_paths_keep_raw_html():
    # markdownBlock, iframe, and fenced code are intentionally raw and must NOT
    # be entity-escaped.
    html = '<iframe src="https://embed.example/x?a=1&b=2"></iframe>'
    assert to_markdown([{"_type": "iframe", "code": html}]) == html
    md = "<div>raw & <b>bold</b></div>"
    assert to_markdown([{"_type": "markdownBlock", "markdown": md}]) == md
    code = [{"_type": "code", "code": "x = a < b && c", "language": "js"}]
    assert to_markdown(code) == "```js\nx = a < b && c\n```"


def test_thematic_break_line_is_escaped():
    # A paragraph that is only dashes would render as a horizontal rule.
    assert to_markdown([_block("normal", "---")]) == "\\---"
    assert to_markdown([_block("normal", "===")]) == "\\==="


def test_escape_text_neutralizes_inline_syntax():
    # Inline metacharacters and HTML are escaped; a leading ``#`` is not, since
    # this helper feeds text that is not at a line start (e.g. after ``# `` in a
    # title heading).
    from libs.sanity.portable_text import escape_text

    assert escape_text("# [x](y) <b> & *z*") == (
        "# \\[x\\]\\(y\\) &lt;b&gt; &amp; \\*z\\*"
    )


def test_verbatim_block_trailing_whitespace_preserved():
    # The document-wide strip is gone, so a markdownBlock's significant trailing
    # whitespace survives byte-for-byte.
    md = "| a |\n|---|\n\n"
    assert to_markdown([{"_type": "markdownBlock", "markdown": md}]) == md


def test_unknown_block_type_cannot_break_out_of_comment():
    out = to_markdown([{"_type": "a-->b<c>"}])
    assert out == "<!-- unsupported Portable Text block: a-bc -->"
    assert "-->" not in out[:-3]  # no early comment close


def test_empty_code_block_preserved_as_empty_fence():
    assert (
        to_markdown([{"_type": "code", "code": "", "language": "py"}]) == "```py\n\n```"
    )


def test_code_block_without_code_key_is_skipped():
    assert to_markdown([{"_type": "code"}]) == ""


def test_empty_markdown_block_preserved_between_blocks():
    blocks = [
        _block("normal", "a"),
        {"_type": "markdownBlock", "markdown": ""},
        _block("normal", "b"),
    ]
    # The empty block contributes an (empty) element rather than vanishing, so
    # the gap between a and b widens instead of collapsing to a single break.
    assert to_markdown(blocks) == "a\n\n\n\nb"


def test_code_block_drops_unsafe_language():
    blocks = [{"_type": "code", "code": "x", "language": "py thon`"}]
    assert to_markdown(blocks) == "```\nx\n```"


def test_code_block_keeps_valid_language_with_symbols():
    blocks = [{"_type": "code", "code": "x", "language": "c++"}]
    assert to_markdown(blocks) == "```c++\nx\n```"


def test_span_newline_becomes_hard_break():
    blocks = [_block("normal", "line one\nline two")]
    assert to_markdown(blocks) == "line one  \nline two"


def test_block_syntax_escaped_on_every_line_of_a_span():
    blocks = [_block("normal", "foo\n# bar\n- baz")]
    assert to_markdown(blocks) == "foo  \n\\# bar  \n\\- baz"


def test_heading_block_trailing_hash_escaped():
    assert to_markdown([_block("h2", "Release #")]) == "## Release \\#"


def test_heading_block_collapses_hard_breaks():
    assert to_markdown([_block("h2", "line one\nline two")]) == "## line one line two"


def test_multiline_blockquote_prefixes_every_line():
    blocks = [_block("blockquote", "line one\nline two")]
    assert to_markdown(blocks) == "> line one  \n> line two"


def test_multiline_list_item_indents_continuation():
    blocks = [_block("normal", "line one\nline two", listItem="bullet", level=1)]
    assert to_markdown(blocks) == "- line one  \n  line two"


def test_multiline_numbered_item_indents_continuation():
    blocks = [_block("normal", "a\nb", listItem="number", level=1)]
    assert to_markdown(blocks) == "1. a  \n   b"


def test_inline_code_with_edge_spaces_is_padded():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [],
        "children": [{"_type": "span", "marks": ["code"], "text": " x "}],
    }
    # CommonMark strips one space from each side of " x "; padding round-trips it.
    assert to_markdown([block]) == "`  x  `"


def test_inline_code_all_spaces_is_not_padded():
    block = {
        "_type": "block",
        "style": "normal",
        "markDefs": [],
        "children": [{"_type": "span", "marks": ["code"], "text": "  "}],
    }
    assert to_markdown([block]) == "`  `"
