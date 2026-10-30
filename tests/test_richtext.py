"""HTML and Markdown typed as text become Telegram formatting (app/richtext.py) - and everything else stays as typed."""
import html
import random
import re
import time
from types import SimpleNamespace

import pytest
from telethon import types
from telethon.extensions import html as telethon_html

from app import richtext
from app.common import UserError
from app.handlers.panel import extract_content, markup_note
from app.richtext import CHEAT_SHEET, clean_url, convert, from_html, from_markdown, looks_like_html, message_text
from app.tgutil import de_entities, ser_entities, utf16_len

from .harness import App, norm


def piece(text, e):
    """The text an entity covers, found the way Telegram counts: in UTF-16 units."""
    raw = text.encode("utf-16-le")
    return raw[e["o"] * 2 : (e["o"] + e["l"]) * 2].decode("utf-16-le")


def read(src):
    """(text, [(kind, covered text)] outer first, how it was read)"""
    text, ents, how = convert(src)
    ents = sorted(ents, key=lambda e: (e["o"], -e["l"], e["k"]))
    return text, [(e["k"], piece(text, e)) for e in ents], how


def entity(src, kind, **extra):
    """The one entity of `kind` in `src`, as stored."""
    text, ents, _ = convert(src)
    (found,) = [e for e in ents if e["k"] == kind]
    for key, value in extra.items():
        assert found[key] == value, (found, key)
    return found


def msg(text, entities=None):
    return SimpleNamespace(raw_text=text, entities=entities or [], media=None, grouped_id=None, forward=None)


# --------------------------------------------------------------------------------------------- Markdown marks
@pytest.mark.parametrize(
    "src, text, found",
    [
        ("**bold**", "bold", [("bold", "bold")]),
        ("*it*", "it", [("italic", "it")]),
        ("_it_", "it", [("italic", "it")]),
        ("__under__", "under", [("underline", "under")]),
        ("~~gone~~", "gone", [("strike", "gone")]),
        ("||secret||", "secret", [("spoiler", "secret")]),
        ("`code`", "code", [("code", "code")]),
        ("**bold** *it* _it2_ __under__ ~~gone~~ ||secret|| `code`", "bold it it2 under gone secret code",
         [("bold", "bold"), ("italic", "it"), ("italic", "it2"), ("underline", "under"), ("strike", "gone"), ("spoiler", "secret"), ("code", "code")]),
        ("***both***", "both", [("bold", "both"), ("italic", "both")]),
        ("**bold *and italic* inside**", "bold and italic inside", [("bold", "bold and italic inside"), ("italic", "and italic")]),
        ("~~**both**~~", "both", [("bold", "both"), ("strike", "both")]),
        ("||**sp bold**||", "sp bold", [("bold", "sp bold"), ("spoiler", "sp bold")]),
        ("||a|b||", "a|b", [("spoiler", "a|b")]),
        ("**a** and **b**", "a and b", [("bold", "a"), ("bold", "b")]),
        ("**bold**text", "boldtext", [("bold", "bold")]),  # ** works inside a word, like in Telegram's own Markdown
        ("(_paren_)", "(paren)", [("italic", "paren")]),
        ("- item *x*", "- item x", [("italic", "x")]),
        ("  **lead space**  ", "  lead space  ", [("bold", "lead space")]),
        ("`**not bold**`", "**not bold**", [("code", "**not bold**")]),  # nothing is read inside code
        ("`` a`b ``", " a`b ", [("code", " a`b ")]),  # a longer fence lets a ` be part of the code
        ("line1\nline2\n\nline4 **b**", "line1\nline2\n\nline4 b", [("bold", "b")]),
        ("नमस्ते *दुनिया*", "नमस्ते दुनिया", [("italic", "दुनिया")]),
    ],
)
def test_markdown_marks_become_formatting(src, text, found):
    assert read(src) == (text, found, "markdown")


@pytest.mark.parametrize(
    "src",
    [
        "plain text",
        "5 * 3 = 15",
        "price: $5 * 2 and $6 * 3",
        "2 ** 3 ** 4",
        "snake_case_names and a*b*c",
        "a_b_c_d",
        "foo_bar_ baz",
        "__a__b",
        "x ~y~ z",
        "|||x|||",
        "Sub | Dub | Hindi",
        "<3 [Group] Title",
        r"C:\Users\me\file_name.txt",
        "* list item\n* another",
        ">> Download <<",
        "[x](nonsense)",
        "[x](javascript:alert(1))",
        "[ ](https://x.com)",
        "`unclosed",
        "```python\nunclosed fence",
        "``````",
        "https://example.com/a_b_c",
        "just <b> a lone tag",
        "<b>never closed",
        "1 < 2 > 0",
    ],
)
def test_text_without_formatting_is_left_exactly_as_typed(src):
    assert convert(src) == (src, [], None)


def test_marks_that_pair_with_nothing_stay_in_the_text():
    assert read("**bold* x") == ("*bold x", [("italic", "bold")], "markdown")  # one * closes the first, one is left over
    assert read("a *b") == ("a *b", [], None)
    assert read("**a** and **b") == ("a and **b", [("bold", "a")], "markdown")


def test_a_mark_needs_to_touch_the_text_it_formats():
    assert convert("** not bold **") == ("** not bold **", [], None)
    assert convert("* not italic *") == ("* not italic *", [], None)
    assert read("**bold** and ** not **") == ("bold and ** not **", [("bold", "bold")], "markdown")


# -------------------------------------------------------------------------------------------------- escapes
def test_a_backslash_keeps_the_next_mark_as_typed():
    assert read("\\*not italic\\*") == ("*not italic*", [], "markdown")
    assert read("\\# not heading \\[x\\] \\`t\\`") == ("# not heading [x] `t`", [], "markdown")
    assert read("**a \\*b\\* c**") == ("a *b* c", [("bold", "a *b* c")], "markdown")
    assert read("\\\\**x**") == ("\\x", [("bold", "x")], "markdown")  # an escaped backslash does not escape the mark
    assert read("\\_\\_x\\_\\_") == ("__x__", [], "markdown")


def test_a_backslash_before_anything_else_is_just_a_backslash():
    assert read("x\\—y *a*") == ("x\\—y a", [("italic", "a")], "markdown")
    assert read("path\\to *x*") == ("path\\to x", [("italic", "x")], "markdown")


# ---------------------------------------------------------------------------------------------------- links
@pytest.mark.parametrize(
    "src, label, url",
    [
        ("[text](https://t.me/name)", "text", "https://t.me/name"),
        ("[x](t.me/name)", "x", "https://t.me/name"),  # a link without https:// gets it
        ("[x](example.com/path?q=1)", "x", "https://example.com/path?q=1"),
        ("[x](tg://resolve?domain=x)", "x", "tg://resolve?domain=x"),
        ("[w](https://en.wikipedia.org/wiki/Foo_(bar))", "w", "https://en.wikipedia.org/wiki/Foo_(bar)"),
        ("[a\\]b](https://x.com)", "a]b", "https://x.com"),
        ("[**bold link**](https://x.com)", "bold link", "https://x.com"),
    ],
)
def test_markdown_links(src, label, url):
    text, found, how = read(src)
    assert (text, how) == (label, "markdown") and ("url", label) in found
    assert entity(src, "url", u=url)["l"] == utf16_len(label)


def test_two_links_in_one_line_and_a_link_next_to_text():
    text, found, _ = read("[a](https://x.com) and [b](https://y.com)")
    assert (text, found) == ("a and b", [("url", "a"), ("url", "b")])
    assert [e["u"] for e in convert("[a](https://x.com) and [b](https://y.com)")[1]] == ["https://x.com", "https://y.com"]
    assert read("see [here](https://x.com)!") == ("see here!", [("url", "here")], "markdown")


def test_a_link_typed_as_it_is_is_not_cut_up_by_marks_inside_it():
    assert convert("https://example.com/a_b_c and https://t.me/x_y_z?start=1") == (
        "https://example.com/a_b_c and https://t.me/x_y_z?start=1", [], None)
    text, found, _ = read("**https://example.com/a_b**")
    assert text == "https://example.com/a_b" and found == [("bold", "https://example.com/a_b")]
    assert read("_see https://example.com/x_y now_") == ("see https://example.com/x_y now", [("italic", "see https://example.com/x_y now")], "markdown")


@pytest.mark.parametrize("raw", ["https://t.me/x", "t.me/x", "tg://resolve?domain=x", "ton://transfer/abc", "example.com/a?b=c#d", "  https://x.com  "])
def test_clean_url_accepts_links(raw):
    url = clean_url(raw)
    assert url is not None and url.startswith(("https://", "http://", "tg://", "ton://"))


@pytest.mark.parametrize("raw", [None, "", "x", "nonsense", "javascript:alert(1)", "https://", "tg://", "a b", "https://x.com/a b", "https://x.com/" + "a" * 2001, "file:///etc/passwd"])
def test_clean_url_refuses_what_is_no_link(raw):
    assert clean_url(raw) is None


# --------------------------------------------------------------------------------------------------- quotes
def test_a_line_that_starts_with_a_greater_than_sign_is_quoted():
    assert read("> quote\nplain") == ("quote\nplain", [("quote", "quote")], "markdown")
    assert entity("> quote\nplain", "quote", c=False)
    assert read(">no space") == ("no space", [("quote", "no space")], "markdown")


def test_following_lines_make_one_quote_and_a_gap_ends_it():
    assert read("> a\n> b\n\nplain\n> c") == ("a\nb\n\nplain\nc", [("quote", "a\nb"), ("quote", "c")], "markdown")
    assert read("intro\n> a\n> b\noutro") == ("intro\na\nb\noutro", [("quote", "a\nb")], "markdown")


def test_a_quote_may_hold_other_formatting():
    assert read("> **bold** inside") == ("bold inside", [("quote", "bold inside"), ("bold", "bold")], "markdown")


def test_an_expandable_quote_starts_with_and_ends_with_the_marks():
    assert read("**> exp\n> more||\nafter") == ("exp\nmore\nafter", [("quote", "exp\nmore")], "markdown")
    assert entity("**> exp\n> more||\nafter", "quote", c=True)
    assert entity("**> only one line||", "quote", c=True)


def test_two_greater_than_signs_are_decoration_not_a_quote():
    assert convert(">> Download <<") == (">> Download <<", [], None)
    assert read(">> Download <<\n**x**") == (">> Download <<\nx", [("bold", "x")], "markdown")


def test_text_before_a_quote_keeps_its_offsets():
    text, ents, _ = convert("🙂 head\n> quoted *it*\ntail")
    assert text == "🙂 head\nquoted it\ntail"
    assert [(e["k"], piece(text, e)) for e in sorted(ents, key=lambda e: (e["o"], -e["l"]))] == [("quote", "quoted it"), ("italic", "it")]


# -------------------------------------------------------------------------------------------- code blocks
def test_fenced_code_with_and_without_a_language():
    assert read("```python\nprint(1)\n```") == ("print(1)", [("pre", "print(1)")], "markdown")
    assert entity("```python\nprint(1)\n```", "pre", g="python")
    assert entity("```\nx = 1\n```", "pre", g="")
    assert entity("```code```", "pre", g="")
    assert entity("```c++\nint x;\n```", "pre", g="c++")


def test_text_around_a_code_block_is_read_and_the_code_inside_is_not():
    text, found, how = read("intro **a**\n```py\n**x**\nb\n```\noutro *c*")
    assert how == "markdown" and text == "intro a\n**x**\nb\noutro c"
    assert found == [("bold", "a"), ("pre", "**x**\nb"), ("italic", "c")]


def test_an_unclosed_fence_is_text():
    assert convert("```python\nx") == ("```python\nx", [], None)


# ------------------------------------------------------------------------------------------------ emoji
def test_a_custom_emoji_is_written_like_an_image_link():
    text, found, how = read("![🙂](tg://emoji?id=5368324170671202286)")
    assert (text, found, how) == ("🙂", [("emoji", "🙂")], "markdown")
    assert entity("![🙂](tg://emoji?id=5368324170671202286)", "emoji", d=5368324170671202286)["l"] == 2  # two UTF-16 units


def test_offsets_count_utf16_units_not_characters():
    text, ents, _ = convert("🙂🙂 **bold** ![🙂](tg://emoji?id=1) _it_")
    assert text == "🙂🙂 bold 🙂 it"
    by_kind = {e["k"]: e for e in ents}
    assert (by_kind["bold"]["o"], by_kind["bold"]["l"]) == (5, 4)  # 2 emoji x 2 units + a space
    assert (by_kind["emoji"]["o"], by_kind["emoji"]["l"]) == (10, 2)
    assert (by_kind["italic"]["o"], by_kind["italic"]["l"]) == (13, 2)
    assert all(piece(text, e) for e in ents)


# -------------------------------------------------------------------------------------------------- HTML
def test_html_tags_become_formatting():
    src = ('<b>bold</b> <i>it</i> <u>un</u> <s>st</s> <tg-spoiler>sp</tg-spoiler> <code>c</code> <pre>block</pre> '
           '<a href="https://t.me/x">link</a> <blockquote>q</blockquote>')
    assert read(src) == (
        "bold it un st sp c block link q",
        [("bold", "bold"), ("italic", "it"), ("underline", "un"), ("strike", "st"), ("spoiler", "sp"), ("code", "c"),
         ("pre", "block"), ("url", "link"), ("quote", "q")],
        "html",
    )
    assert entity(src, "url", u="https://t.me/x") and entity(src, "quote", c=False) and entity(src, "pre", g="")


@pytest.mark.parametrize(
    "src, kind",
    [
        ("<strong>x</strong>", "bold"), ("<em>x</em>", "italic"), ("<ins>x</ins>", "underline"), ("<strike>x</strike>", "strike"),
        ("<del>x</del>", "strike"), ('<span class="tg-spoiler">x</span>', "spoiler"), ("<B>x</B>", "bold"),
    ],
)
def test_the_other_spellings_of_the_tags(src, kind):
    assert read(src) == ("x", [(kind, "x")], "html")


def test_html_entities_and_line_breaks():
    assert read("a &amp; b &lt;c&gt; &quot;d&quot; &#39;e&#39; <b>x</b>") == ("a & b <c> \"d\" 'e' x", [("bold", "x")], "html")
    assert read("a<br/>b<br>c<b>d</b>") == ("a\nb\ncd", [("bold", "d")], "html")
    assert read("<b>bold</b>\n<i>it</i>\n") == ("bold\nit\n", [("bold", "bold"), ("italic", "it")], "html")


def test_nested_tags():
    assert read("<b>a <i>b</i> c</b>") == ("a b c", [("bold", "a b c"), ("italic", "b")], "html")
    assert read("<b><i>x</i></b>") == ("x", [("bold", "x"), ("italic", "x")], "html")
    assert read('<a href="https://x.com"><b>x</b></a>') == ("x", [("bold", "x"), ("url", "x")], "html")


def test_pre_with_a_language_and_blockquote_expandable_and_custom_emoji():
    assert entity('<pre><code class="language-python">x = 1</code></pre>', "pre", g="python")
    assert read('<pre><code class="language-python">x = 1</code></pre>') == ("x = 1", [("pre", "x = 1")], "html")
    assert entity("<pre><code>no lang</code></pre>", "pre", g="")
    assert entity("<blockquote expandable>long</blockquote>", "quote", c=True)
    assert entity("<blockquote>a\nb</blockquote>", "quote", c=False)["l"] == 3
    assert entity('<tg-emoji emoji-id="123">🙂</tg-emoji>', "emoji", d=123)["l"] == 2
    assert read('<tg-emoji emoji-id="abc">🙂</tg-emoji><b>y</b>') == ("🙂y", [("bold", "y")], "html")  # no usable id: plain emoji


def test_code_is_shown_as_typed_even_with_tags_in_it():
    assert read("<code>&lt;b&gt;</code>") == ("<b>", [("code", "<b>")], "html")
    assert read("<pre><b>x</b></pre>") == ("<b>x</b>", [("pre", "<b>x</b>")], "html")
    assert read("<code>a</code><code>b</code>") == ("ab", [("code", "a"), ("code", "b")], "html")


def test_tags_the_bot_does_not_know_stay_as_typed():
    assert read("<b>x</b> <div>y</div>") == ("x <div>y</div>", [("bold", "x")], "html")
    assert read('<span class="other">x</span><b>y</b>') == ('<span class="other">x</span>y', [("bold", "y")], "html")
    assert read("1 < 2 and <b>x</b>") == ("1 < 2 and x", [("bold", "x")], "html")


def test_a_tag_of_the_same_kind_inside_another_adds_nothing():
    assert read("<b>a <b>b</b> c</b>") == ("a b c", [("bold", "a b c")], "html")
    assert read("<strong>a <b>b</b></strong>") == ("a b", [("bold", "a b")], "html")
    assert read("<blockquote>a<blockquote>b</blockquote></blockquote>") == ("ab", [("quote", "ab")], "html")
    nested_links = '<a href="https://x.com">a <a href="https://y.com">b</a></a>'
    assert read(nested_links) == ("a b", [("url", "a b")], "html") and entity(nested_links, "url", u="https://x.com")
    # other kinds still nest, and two separate ones stay two
    assert read("<b>a <i>b</i></b> <b>c</b>") == ("a b c", [("bold", "a b"), ("italic", "b"), ("bold", "c")], "html")


def test_unclosed_and_stray_tags_do_no_harm():
    assert read("<i>unclosed <b>x</b>") == ("unclosed x", [("italic", "unclosed x"), ("bold", "x")], "html")  # ends with the text
    assert read("<b>x</b></i> stray") == ("x</i> stray", [("bold", "x")], "html")
    assert read("<b></b>x<i>y</i>") == ("xy", [("italic", "y")], "html")  # an empty tag formats nothing


def test_unsafe_or_missing_links_lose_the_link_but_keep_the_text():
    assert read('<a href="javascript:alert(1)">x</a><b>y</b>') == ("xy", [("bold", "y")], "html")
    assert read("<a>no href</a><b>y</b>") == ("no hrefy", [("bold", "y")], "html")
    assert entity('<a href="t.me/x">y</a>', "url", u="https://t.me/x")


@pytest.mark.parametrize(
    "src, yes",
    [
        ("<b>x</b>", True), ("<B>x</B>", True), ('<a href="x">y</a>', True), ("<blockquote expandable>q</blockquote>", True),
        ("<i>a</i> and <b>b</b>", True), ("<tg-spoiler>s</tg-spoiler>", True), ("<pre><code>x</code></pre>", True),
        ("<b>x", False), ("x </b>", False), ("a < b > c", False), ("<3", False), ("<div>x</div>", False), ("**x**", False), ("", False),
    ],
)
def test_html_is_recognised_by_a_tag_with_its_closing_tag(src, yes):
    assert looks_like_html(src) is yes


def test_html_wins_over_markdown_in_the_same_text():
    assert read("<b>x</b> **y**") == ("x **y**", [("bold", "x")], "html")


# ------------------------------------------------------------------------------------------- the choice
def test_text_with_nothing_to_read_comes_back_untouched():
    for src in ("", "hello", "  spaced  ", "line\n\nbreak", "100% sure #tag @name /command"):
        assert convert(src) == (src, [], None)


def test_a_reader_that_fails_leaves_the_text_as_typed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(richtext, "from_markdown", boom)
    assert convert("**x**") == ("**x**", [], None)
    monkeypatch.setattr(richtext, "from_html", boom)
    assert convert("<b>x</b>") == ("<b>x</b>", [], None)


# ----------------------------------------------------------------- what the bot does with a message
def test_telegram_formatting_wins_over_anything_typed():
    ents = [types.MessageEntityItalic(0, 5), types.MessageEntityTextUrl(6, 4, "https://x.com"), types.MessageEntityBlockquote(11, 3, True)]
    text, stored, how = message_text(msg("**x** <b>y</b> z", ents))
    assert text == "**x** <b>y</b> z" and how is None
    assert stored == [{"k": "italic", "o": 0, "l": 5}, {"k": "url", "o": 6, "l": 4, "u": "https://x.com"}, {"k": "quote", "o": 11, "l": 3, "c": True}]


def test_links_and_mentions_telegram_finds_by_itself_are_not_formatting():
    src = "see https://x.com and @name and **bold**"
    auto = [types.MessageEntityUrl(4, 13), types.MessageEntityMention(22, 5)]
    text, stored, how = message_text(msg(src, auto))
    assert how == "markdown" and text == "see https://x.com and @name and bold"
    assert [(e["k"], piece(text, e)) for e in stored] == [("bold", "bold")]


def test_typed_formatting_can_be_switched_off():
    assert message_text(msg("**x** <b>y</b>"), enabled=False) == ("**x** <b>y</b>", [], None)
    assert message_text(msg("**x**"), enabled=True)[2] == "markdown"


def test_empty_messages():
    assert message_text(msg("")) == ("", [], None)
    assert message_text(SimpleNamespace(raw_text=None, entities=None)) == ("", [], None)


def test_extract_content_says_how_the_text_was_read():
    c = extract_content(msg("**Hi** there"))
    assert (c["text"], c["entities"], c.how) == ("Hi there", [{"k": "bold", "o": 0, "l": 2}], "markdown")
    assert c["media_kind"] is None and c["media_file_id"] is None
    assert extract_content(msg("<i>Hi</i>")).how == "html"
    assert extract_content(msg("plain")).how is None
    assert extract_content(msg("**Hi**"), markup=False)["text"] == "**Hi**"
    native = extract_content(msg("**Hi**", [types.MessageEntityBold(0, 6)]))
    assert native["text"] == "**Hi**" and native.how is None and native["entities"] == [{"k": "bold", "o": 0, "l": 6}]


def test_the_note_names_the_format_that_was_read():
    assert markup_note(None) == "" and markup_note(None, "\n") == ""
    assert markup_note("markdown").startswith("🎨 Formatting read from Markdown") and markup_note("markdown").endswith("\n\n")
    assert markup_note("html", "\n").startswith("🎨 Formatting read from HTML") and markup_note("html", "\n").endswith("/formatting.\n")


def test_the_length_limit_counts_the_text_the_formatting_leaves():
    assert len(extract_content(msg("**" + "x" * 4094 + "**"))["text"]) == 4094  # the marks do not count
    with pytest.raises(UserError):
        extract_content(msg("x" * 4097))
    with pytest.raises(UserError):
        extract_content(msg("<b></b>"))  # nothing is left once the empty tag is read


# --------------------------------------------------------------------------- any input, always usable
KINDS = {"bold", "italic", "underline", "strike", "spoiler", "code", "pre", "url", "quote", "emoji"}
TOKENS = [
    "*", "**", "***", "_", "__", "~~", "~", "||", "|", "`", "``", "```", "```py\n", "[", "]", "(", ")", "](", "[a](https://x.com)",
    "[a](t.me/x)", "![🙂](tg://emoji?id=5)", "!", "\\", "\\*", ">", ">>", "**>", "\n", "\n\n", " ", "a", "bc", "dé", "🙂", "नमस्ते",
    "https://x.com/a_b_c", "http://", "tg://x", "<", "<b>", "</b>", "<i>", "</i>", "<u>", "</u>", "<s>", "</s>", "<code>", "</code>",
    "<pre>", "</pre>", "<pre><code class=\"language-py\">", "</code></pre>", "<blockquote>", "<blockquote expandable>", "</blockquote>",
    "<a href=\"https://x.com\">", "<a href=\"nope\">", "</a>", "<tg-spoiler>", "</tg-spoiler>", "<span class=\"tg-spoiler\">", "</span>",
    "<tg-emoji emoji-id=\"7\">", "</tg-emoji>", "<br>", "<br/>", "&amp;", "&lt;", "&gt;", "&", "&#128512;", "<!--", "-->", "<?x>", "<!doctype>",
]


def is_subsequence(small, big):
    it = iter(big)
    return all(ch in it for ch in small)


def check_entities(text, ents):
    n = utf16_len(text)
    last = (-1, 0)
    for e in ents:
        assert e["k"] in KINDS, e
        assert e["l"] > 0 and e["o"] >= 0 and e["o"] + e["l"] <= n, (text, e)
        assert (e["o"], -e["l"]) >= last, "outer entities first"
        last = (e["o"], -e["l"])
        if e["k"] == "url":
            assert e["u"].startswith(("http://", "https://", "tg://", "ton://")), e
    for i, a in enumerate(ents):  # entities are nested or apart, never partly over each other; none sits in one of its own kind
        for b in ents[i + 1:]:
            a_end, b_end = a["o"] + a["l"], b["o"] + b["l"]
            nested = (a["o"] <= b["o"] and b_end <= a_end) or (b["o"] <= a["o"] and a_end <= b_end)
            assert not (b["o"] < a_end and a["o"] < b_end) or nested, (text, a, b)
            if a["k"] == b["k"] and a["k"] in richtext._NO_NEST:
                assert not nested, (text, a, b)
    assert len(de_entities(ents)) == len(ents)  # Telethon can build every one of them


def test_whatever_is_typed_the_readers_never_fail_and_never_point_outside_the_text():
    rng = random.Random(20261029)
    for _ in range(4000):
        src = "".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 40)))
        text, ents = from_markdown(src)  # called directly: convert() would hide a crash
        check_entities(text, ents)
        assert is_subsequence(text, src), (src, text)  # the Markdown reader only takes marks away, it invents nothing
        text, ents = from_html(src)
        check_entities(text, ents)
        text, ents, how = convert(src)
        check_entities(text, ents)
        assert how in (None, "html", "markdown")
        if how is None:
            assert (text, ents) == (src, [])


@pytest.mark.parametrize(
    "src",
    ["[" * 4000, "*" * 4000, "_" * 4000, "`" * 4000, "**a " * 1000, "[a](" * 1000, "a*" * 2000, "![" * 2000, "\\" * 4000, "(" * 4000,
     "> " * 2000, "||a " * 1300, "~~a " * 1300, "http://" * 500, "[a](https://x.com" * 200, "<b>" * 1300 + "</b>" * 1300,
     '<a href="x">' * 300 + "</a>" * 300, "<" * 4000, "&" * 4000, "*a_" * 1300 + "_a*" * 1300, "[[[a]]](" * 500],
    ids=lambda s: f"{s[:8]!r}x{len(s)}",
)
def test_a_hostile_message_is_read_quickly(src):
    started = time.monotonic()
    text, ents, _ = convert(src)
    assert time.monotonic() - started < 3.0
    check_entities(text, ents)


# ---------------------------------------------------------------------------------------- the cheat sheet
def test_the_cheat_sheet_is_valid_telegram_html_and_fits_one_message():
    text, ents = telethon_html.parse(CHEAT_SHEET)
    assert 0 < len(text) < 4096 and len(ents) <= 100
    assert all(0 <= e.offset and e.offset + e.length <= utf16_len(text) for e in ents)
    assert "Markdown" in text and "HTML" in text and "/formatting" not in text


@pytest.mark.parametrize(
    "snippet, kind",
    [
        ("**bold**", "bold"), ("*italic*", "italic"), ("_italic_", "italic"), ("__underline__", "underline"), ("~~strike~~", "strike"),
        ("||spoiler||", "spoiler"), ("`code`", "code"), ("```code block```", "pre"), ("[text](https://t.me/name)", "url"), ("> quote", "quote"),
        ("<b>bold</b>", "bold"), ("<i>italic</i>", "italic"), ("<u>underline</u>", "underline"), ("<s>strike</s>", "strike"),
        ("<tg-spoiler>spoiler</tg-spoiler>", "spoiler"), ("<code>code</code>", "code"), ("<pre>block</pre>", "pre"),
        ('<a href="https://t.me/name">link</a>', "url"), ("<blockquote>quote</blockquote>", "quote"),
    ],
)
def test_every_example_in_the_cheat_sheet_does_what_it_says(snippet, kind):
    shown = {html.unescape(s) for s in re.findall(r"<code>(.*?)</code>", CHEAT_SHEET, re.S)}
    assert snippet in shown, "the example is not in the cheat sheet any more"
    assert kind in {k for k, _ in read(snippet)[1]}


def test_the_example_post_in_the_cheat_sheet_reads_as_it_promises():
    example = html.unescape(re.search(r"<pre>(.*?)</pre>", CHEAT_SHEET, re.S).group(1))
    text, found, how = read(example)
    assert how == "markdown"
    assert found == [("bold", "Attack on Titan"), ("underline", "Hindi"), ("spoiler", "spoiler"), ("url", "Watch here")]
    assert entity(example, "url", u="https://t.me/yourchannel")


def test_the_formatting_command_sends_the_cheat_sheet_and_the_help_lists_it(tmp_path):
    async def go():
        app = App(tmp_path)
        await app.start()
        await app.text("/formatting")
        assert app.out.last_text == CHEAT_SHEET
        await app.text("/help")
        assert "/formatting" in norm(app.out.last_text)
        await app.db.close()

    import asyncio

    asyncio.run(go())


def test_stored_entities_survive_the_round_trip_through_telethon():
    text, stored, _ = convert("**a** [b](https://x.com) > not a quote here\n> q\n```py\nx\n```")
    again = ser_entities(de_entities(stored))
    assert again == stored
