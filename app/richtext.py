"""Telegram formatting from text typed as HTML or Markdown - for when Telegram's own editor is not at hand.

A message the owner sends can carry Telegram's own formatting: that is used exactly as it is. When it carries none, the
text is read as HTML (when it holds a supported tag together with its closing tag) or else as Markdown, and what is
recognised becomes real formatting. Whatever is not recognised stays as it was typed, so ordinary text (``5 * 3``,
``snake_case_names``, ``<3``, ``[Group] Title``, ``Sub | Dub``) is left alone.

HTML, like Telegram's own HTML mode:
    <b> <strong>, <i> <em>, <u> <ins>, <s> <strike> <del>, <tg-spoiler> or <span class="tg-spoiler">, <code>,
    <pre> (<pre><code class="language-python">..</code></pre> names a language), <a href="..">, <blockquote> and
    <blockquote expandable>, <tg-emoji emoji-id="123">🙂</tg-emoji>, <br> for a line break.
    ``&lt;`` ``&gt;`` ``&amp;`` stand for those characters. Any other tag is left as typed.

Markdown:
    **bold**  *italic*  _italic_  __underline__  ~~strikethrough~~  ||spoiler||  `code`  ```code block```
    [text](https://link)  ![🙂](tg://emoji?id=123)  a line that starts with ``>`` is quoted  (``**>`` starts an
    expandable quote that ends with ``||``)  and a backslash before a punctuation mark keeps it as it is.
    As in CommonMark, a mark must touch the text it formats (``**bold**``, not ``** bold **``), and a lone ``*`` or
    ``_`` inside a word does nothing. A link typed as it is (https://...) is never cut up by marks inside it.

Entities come out in the form the bot stores them (see ``tgutil.ser_entities``), with UTF-16 offsets like Telegram's.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from .tgutil import ser_entities, utf16_len

log = logging.getLogger(__name__)

Span = Tuple[str, int, int, object]  # (kind, offset, length, arg) - offsets in UTF-16 units, like Telegram's own


def _entity(kind: str, offset: int, length: int, arg=None) -> dict:
    """One entity as the bot stores it; `arg` is the pre block's language, the link, the emoji id or "expandable"."""
    e = {"k": kind, "o": offset, "l": length}
    if kind == "pre":
        e["g"] = arg or ""
    elif kind == "url":
        e["u"] = arg
    elif kind == "quote":
        e["c"] = bool(arg)
    elif kind == "emoji":
        e["d"] = arg
    return e


_NO_NEST = {"bold", "italic", "underline", "strike", "spoiler", "code", "quote", "url"}  # a second one inside the first adds nothing


def _entities(spans: List[Span]) -> list:
    """Entities for the spans, outer ones first (same start: the longer one).

    A span of a kind that lies wholly inside another of the same kind (``<b>a <b>b</b></b>``, a quote in a quote) is left
    out: it changes nothing on screen, and Telegram does not want such entities nested."""
    out: list = []
    kept: List[Tuple[str, int, int]] = []
    for kind, offset, length, arg in sorted((s for s in spans if s[2] > 0), key=lambda s: (s[1], -s[2])):
        if kind in _NO_NEST and any(k == kind and offset >= a and offset + length <= b for k, a, b in kept):
            continue
        kept.append((kind, offset, offset + length))
        out.append(_entity(kind, offset, length, arg))
    return out


# ------------------------------------------------------------------------------------------------------ links
_SCHEMES = ("http://", "https://", "tg://", "ton://")
_DOMAIN = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#]\S*)?")


def clean_url(raw: Optional[str]) -> Optional[str]:
    """The link as Telegram wants it, or None when it is no link (``t.me/x`` gets its https://)."""
    url = (raw or "").strip()
    if not url or len(url) > 2000 or any(c.isspace() for c in url):
        return None
    low = url.lower()
    for scheme in _SCHEMES:
        if low.startswith(scheme):
            return url if len(url) > len(scheme) else None
    return "https://" + url if _DOMAIN.fullmatch(url) else None


# ------------------------------------------------------------------------------------------------------- HTML
_TAGS = ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote", "tg-spoiler", "tg-emoji", "span")
_HTML_PAIR = re.compile(r"<(" + "|".join(re.escape(t) for t in _TAGS) + r")(?:\s[^<>]*)?>.*?</\1\s*>", re.I | re.S)
_KIND_OF_TAG = {
    "b": "bold", "strong": "bold", "i": "italic", "em": "italic", "u": "underline", "ins": "underline",
    "s": "strike", "strike": "strike", "del": "strike", "tg-spoiler": "spoiler", "span": "spoiler",
}


def looks_like_html(text: str) -> bool:
    """A supported tag with its closing tag is there (a lone ``<b>`` in a sentence is just text)."""
    return bool(_HTML_PAIR.search(text))


class _Html(HTMLParser):
    """Collects the text and the formatting of an HTML message; what it does not know is kept as it was typed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.pos = 0  # UTF-16 length of the text so far
        self.open: List[dict] = []
        self.spans: List[Span] = []

    def text(self, s: str) -> None:
        if s:
            self.parts.append(s)
            self.pos += utf16_len(s)

    def literal_zone(self) -> bool:
        """Inside <pre> / <code> everything is code, so a tag there is shown as typed."""
        return any(o["tag"] in ("pre", "code") for o in self.open)

    # --- tags
    def handle_starttag(self, tag, attrs):
        t, a = tag.lower(), {k: (v or "") for k, v in attrs}
        raw = self.get_starttag_text() or ""
        if t == "br" and not self.literal_zone():
            self.text("\n")
            return
        if self.literal_zone():
            top = self.open[-1]
            if t == "code" and top["tag"] == "pre" and self.pos == top["start"] and "lang" not in top:
                cls = a.get("class", "")
                top["lang"] = cls[len("language-"):] if cls.startswith("language-") else ""
                self.open.append({"tag": "code", "start": self.pos, "holder": True})
            else:
                self.text(raw)
            return
        if t == "span" and "tg-spoiler" not in a.get("class", "").split():
            self.text(raw)
        elif t in _TAGS:
            self.open.append({"tag": t, "start": self.pos, "attrs": a})
        else:
            self.text(raw)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br" and not self.literal_zone():
            self.text("\n")
        else:
            self.text(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "br":
            return
        idx = next((i for i in range(len(self.open) - 1, -1, -1) if self.open[i]["tag"] == t), None)
        if idx is None:
            self.text(f"</{tag}>")
            return
        while len(self.open) > idx:  # a tag opened inside it and never closed ends with it
            self.close_tag(self.open.pop())

    def close_tag(self, o: dict) -> None:
        length = self.pos - o["start"]
        if length <= 0 or o.get("holder"):
            return
        t, a = o["tag"], o.get("attrs", {})
        if t in _KIND_OF_TAG:
            self.spans.append((_KIND_OF_TAG[t], o["start"], length, None))
        elif t == "code":
            self.spans.append(("code", o["start"], length, None))
        elif t == "pre":
            self.spans.append(("pre", o["start"], length, o.get("lang", "")))
        elif t == "a":
            url = clean_url(a.get("href"))
            if url:
                self.spans.append(("url", o["start"], length, url))
        elif t == "blockquote":
            self.spans.append(("quote", o["start"], length, "expandable" in a))
        elif t == "tg-emoji":
            emoji_id = a.get("emoji-id", "")
            if emoji_id.isdigit():
                self.spans.append(("emoji", o["start"], length, int(emoji_id)))

    # --- text and the rest
    def handle_data(self, data):
        self.text(data)

    def handle_comment(self, data):
        self.text(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.text(f"<!{decl}>")

    def handle_pi(self, data):
        self.text(f"<?{data}>")

    def finish(self) -> Tuple[str, List[Span]]:
        self.close()
        while self.open:  # a tag that was never closed ends with the text
            self.close_tag(self.open.pop())
        return "".join(self.parts), self.spans


def from_html(source: str) -> Tuple[str, list]:
    """(text, entities) of an HTML message."""
    parser = _Html()
    parser.feed(source)
    text, spans = parser.finish()
    return text, _entities(spans)


# --------------------------------------------------------------------------------------------------- Markdown
_FENCE = re.compile(r"```(?:([A-Za-z0-9_+#.-]{1,30})\n)?(.+?)```", re.S)
_EMOJI = re.compile(r"!\[([^\[\]\n]{1,16})\]\(tg://emoji\?id=(\d{1,20})\)")
_BARE_URL = re.compile(r"(?:https?|tg|ton)://[^\s<>]+", re.I)
_URL_END = "?!.,:;*_~|\"'"  # what may follow a link without being part of it (a mark, or the end of a sentence)
_KIND_OF_MARK = {("*", 2): "bold", ("*", 1): "italic", ("_", 2): "underline", ("_", 1): "italic", ("~", 2): "strike", ("|", 2): "spoiler"}
MAX_MARKS = 400  # a message with more marks than this is left as typed (keeps the matching fast)


def _punct(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "PS"


def _flanking(mark: str, run: int, prev: Optional[str], nxt: Optional[str]) -> Tuple[bool, bool]:
    """(can open, can close) for a run of `run` marks between the characters `prev` and `nxt`, by the CommonMark rules:
    it opens when text follows it and closes when text comes before it. A lone ``*`` and ``_`` also need a space or
    punctuation outside, so ``snake_case`` and a censored f*ck ... s*it stay as typed (``**bold**text`` still works)."""
    prev_space, next_space = prev is None or prev.isspace(), nxt is None or nxt.isspace()
    prev_punct, next_punct = prev is not None and _punct(prev), nxt is not None and _punct(nxt)
    left = not next_space and (not next_punct or prev_space or prev_punct)
    right = not prev_space and (not prev_punct or next_space or next_punct)
    if mark == "_" or (mark == "*" and run == 1):
        return left and (not right or prev_punct), right and (not left or next_punct)
    return left, right


def _bare_url(s: str, i: int) -> Optional[str]:
    """A link typed as it is (https://...) that starts at s[i], without what ends the sentence or closes a mark."""
    if i and s[i - 1].isalnum():
        return None
    m = _BARE_URL.match(s, i)
    if not m:
        return None
    url = m.group()
    while url:
        last = url[-1]
        if last in _URL_END or (last in ")]" and url.count(last) > url.count("(" if last == ")" else "[")):
            url = url[:-1]
        else:
            break
    return url if len(url) > url.index("//") + 2 else None


def _link_at(s: str, i: int) -> Optional[Tuple[str, str, int]]:
    """``[label](url)`` that starts at s[i]: (label, url, end) or None."""
    depth, j, limit = 0, i, min(len(s), i + 600)
    while j < limit:
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    else:
        return None
    if j + 1 >= len(s) or s[j + 1] != "(":
        return None
    k, depth, limit = j + 2, 1, min(len(s), j + 2 + 2000)
    while k < limit:
        ch = s[k]
        if ch.isspace():
            return None
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    else:
        return None
    url, label = clean_url(s[j + 2:k]), s[i + 1:j]
    return (label, url, k + 1) if url and label.strip() else None


class _Node:
    """A piece of a paragraph: plain text, a code span, an emoji, a link, or a run of marks ("d")."""

    __slots__ = ("kind", "text", "spans", "arg", "ch", "orig", "left", "can_open", "can_close", "start")

    def __init__(self, kind, text="", spans=None, arg=None, ch="", orig=0, can_open=False, can_close=False):
        self.kind, self.text, self.spans, self.arg = kind, text, spans or [], arg
        self.ch, self.orig, self.left, self.can_open, self.can_close = ch, orig, orig, can_open, can_close
        self.start = 0


def _inline(s: str, nested: bool = False) -> Tuple[str, List[Span]]:
    """Text and formatting of Markdown inside one paragraph (or one quote)."""
    nodes: List[_Node] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            nodes.append(_Node("t", "".join(buf)))
            buf.clear()

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1].isascii() and _punct(s[i + 1]):
            buf.append(s[i + 1])
            i += 2
            continue
        if c == "`":
            run = 1
            while i + run < n and s[i + run] == "`":
                run += 1
            close = re.compile(r"(?<!`)" + "`" * run + r"(?!`)").search(s, i + run)
            if close and close.start() > i + run:
                flush()
                nodes.append(_Node("code", s[i + run:close.start()]))
                i = close.end()
            else:
                buf.append("`" * run)
                i += run
            continue
        if c == "!" and s.startswith("![", i):
            m = _EMOJI.match(s, i)
            if m:
                flush()
                nodes.append(_Node("emoji", m.group(1), arg=int(m.group(2))))
                i = m.end()
                continue
        if c == "[" and not nested:
            link = _link_at(s, i)
            if link:
                flush()
                label, url, end = link
                inner, spans = _inline(label, nested=True)
                nodes.append(_Node("link", inner, spans, url))
                i = end
                continue
        if c in "hHtT":
            url = _bare_url(s, i)
            if url:
                buf.append(url)
                i += len(url)
                continue
        if c in "*_~|":
            run = 1
            while i + run < n and s[i + run] == c:
                run += 1
            if c in "~|" and run != 2:
                buf.append(c * run)
            else:
                flush()
                can_open, can_close = _flanking(c, run, s[i - 1] if i else None, s[i + run] if i + run < n else None)
                nodes.append(_Node("d", ch=c, orig=run, can_open=can_open, can_close=can_close))
            i += run
            continue
        buf.append(c)
        i += 1
    flush()
    return _assemble(nodes)


def _assemble(nodes: List[_Node]) -> Tuple[str, List[Span]]:
    """Match the marks (CommonMark's delimiter-stack way), then lay out the text and the spans."""
    marks = [k for k, nd in enumerate(nodes) if nd.kind == "d"]
    if len(marks) > MAX_MARKS:
        for k in marks:
            nodes[k].can_open = nodes[k].can_close = False
    pairs: List[Tuple[int, int, str]] = []
    for pos, j in enumerate(marks):
        closer = nodes[j]
        while closer.can_close and closer.left > 0:
            found = None
            for kk in range(pos - 1, -1, -1):
                opener = nodes[marks[kk]]
                if opener.ch != closer.ch or not opener.can_open or opener.left <= 0:
                    continue
                # a mark that could do both: runs whose lengths add up to a multiple of 3 do not pair (unless each is one)
                both = (opener.can_close or closer.can_open) and (opener.orig + closer.orig) % 3 == 0
                if closer.ch in "*_" and both and not (opener.orig % 3 == 0 and closer.orig % 3 == 0):
                    continue
                found = kk
                break
            if found is None:
                break
            opener = nodes[marks[found]]
            use = 2 if opener.left >= 2 and closer.left >= 2 else 1
            pairs.append((marks[found], j, _KIND_OF_MARK[(closer.ch, use)]))
            opener.left -= use
            closer.left -= use
            for between in marks[found + 1:pos]:  # what lies between the two can no longer pair across them
                nodes[between].can_open = nodes[between].can_close = False
    out: List[str] = []
    pos = 0
    for nd in nodes:
        nd.start = pos
        piece = nd.ch * nd.left if nd.kind == "d" else nd.text  # marks that found no partner stay as typed
        out.append(piece)
        pos += utf16_len(piece)
    spans: List[Span] = []
    for a, b, kind in pairs:
        begin = nodes[a].start + nodes[a].left
        spans.append((kind, begin, nodes[b].start - begin, None))
    for nd in nodes:
        length = utf16_len(nd.text)
        if nd.kind == "code":
            spans.append(("code", nd.start, length, None))
        elif nd.kind == "emoji":
            spans.append(("emoji", nd.start, length, nd.arg))
        elif nd.kind == "link":
            spans.append(("url", nd.start, length, nd.arg))
            spans.extend((k, nd.start + o, ln, arg) for k, o, ln, arg in nd.spans)
    return "".join(out), spans


def _quote_line(line: str) -> Optional[Tuple[str, bool]]:
    """(the line without its mark, whether it starts an expandable quote) when `line` is part of a quote."""
    expandable = line.startswith("**>")
    if expandable or (line.startswith(">") and not line.startswith(">>")):  # ">> Download <<" is decoration, not a quote
        body = line[3:] if expandable else line[1:]
        return (body[1:] if body.startswith(" ") else body), expandable
    return None


def _blocks(seg: str) -> Tuple[str, List[Span]]:
    """A stretch of Markdown without code blocks: paragraphs and quotes."""
    runs: List[list] = []  # [is_quote, expandable, lines]
    for line in seg.split("\n"):
        q = _quote_line(line)
        if q is None:
            if runs and not runs[-1][0]:
                runs[-1][2].append(line)
            else:
                runs.append([False, False, [line]])
        elif runs and runs[-1][0] and not q[1]:
            runs[-1][2].append(q[0])
        else:
            runs.append([True, q[1], [q[0]]])
    out: List[str] = []
    spans: List[Span] = []
    pos = 0
    for is_quote, expandable, lines in runs:
        if is_quote and expandable and lines[-1].endswith("||"):
            lines[-1] = lines[-1][:-2]
        text, inner = _inline("\n".join(lines))
        spans.extend((kind, pos + o, n, arg) for kind, o, n, arg in inner)
        if is_quote and text.strip():
            spans.append(("quote", pos, utf16_len(text), expandable))
        out.append(text)
        pos += utf16_len(text) + 1  # + the line break that separates it from the next run
    return "\n".join(out), spans


def from_markdown(source: str) -> Tuple[str, list]:
    """(text, entities) of a Markdown message."""
    out: List[str] = []
    spans: List[Span] = []
    pos = 0

    def add(text: str, found: List[Span]) -> None:
        nonlocal pos
        spans.extend((kind, pos + o, n, arg) for kind, o, n, arg in found)
        out.append(text)
        pos += utf16_len(text)

    last = 0
    for m in _FENCE.finditer(source):
        code = m.group(2)
        if m.group(1) is None and code.startswith("\n"):
            code = code[1:]
        if code.endswith("\n"):
            code = code[:-1]
        if not code:
            continue
        add(*_blocks(source[last:m.start()]))
        add(code, [("pre", 0, utf16_len(code), m.group(1) or "")])
        last = m.end()
    add(*_blocks(source[last:]))
    return "".join(out), _entities(spans)


# ------------------------------------------------------------------------------------------- what the bot uses
def convert(text: str) -> Tuple[str, list, Optional[str]]:
    """(text, entities, "html" | "markdown" | None) for text that carries no Telegram formatting.

    Text with nothing to read comes back as it was (the third value None). It never raises: text the readers choke
    on is simply used as typed, so a post is never lost to a formatting quirk.
    """
    try:
        if looks_like_html(text):
            new, ents = from_html(text)
            return new, ents, "html"
        new, ents = from_markdown(text)
    except Exception:  # noqa: BLE001
        log.warning("could not read the formatting of a text - using it as typed", exc_info=True)
        return text, [], None
    return (new, ents, "markdown") if (ents or new != text) else (text, [], None)


def message_text(m, enabled: bool = True) -> Tuple[str, list, Optional[str]]:
    """(text, entities in the form the bot stores, "html" | "markdown" | None) of a message the owner sent.

    Telegram's own formatting wins: a message that has some is kept exactly as it is. Otherwise HTML or Markdown in
    the text becomes formatting (unless `enabled` is False). The last value says which one was read.
    """
    text = m.raw_text or ""
    own = ser_entities(m.entities)  # (links, @mentions, #tags ... that Telegram finds by itself are not formatting)
    if own or not enabled or not text:
        return text, own, None
    return convert(text)


CHEAT_SHEET = (
    "✍️ <b>Formatting</b>\n\n"
    "Send a post with Telegram's own formatting (bold, italic, quote, spoiler, links ...) and it is published exactly "
    "like that. When you can't use Telegram's editor, type <b>Markdown</b> or <b>HTML</b> instead - the bot turns it "
    "into real formatting. A message that already has Telegram formatting is used as it is, and text the bot doesn't "
    "recognise stays as you typed it. 👁 Preview shows the result before you publish.\n\n"
    "<b>Markdown</b>\n"
    "<code>**bold**</code> <code>*italic*</code> <code>_italic_</code> <code>__underline__</code> "
    "<code>~~strike~~</code> <code>||spoiler||</code>\n"
    "<code>`code`</code> and <code>```code block```</code> (<code>```python</code> and a line break names the language)\n"
    "<code>[text](https://t.me/name)</code> - a link\n"
    "<code>&gt; quote</code> - a line that starts with &gt; is quoted (<code>**&gt;</code> on the first line and "
    "<code>||</code> at the end of the last one make it expandable)\n"
    "<code>\\*</code> - a backslash keeps the next mark as it is. As in any Markdown, the marks must touch the text "
    "(<code>**bold**</code>, not <code>** bold **</code>) and <code>_</code> inside a word does nothing.\n\n"
    "<b>HTML</b>\n"
    "<code>&lt;b&gt;bold&lt;/b&gt;</code> <code>&lt;i&gt;italic&lt;/i&gt;</code> "
    "<code>&lt;u&gt;underline&lt;/u&gt;</code> <code>&lt;s&gt;strike&lt;/s&gt;</code> "
    "<code>&lt;tg-spoiler&gt;spoiler&lt;/tg-spoiler&gt;</code>\n"
    "<code>&lt;code&gt;code&lt;/code&gt;</code> <code>&lt;pre&gt;block&lt;/pre&gt;</code> "
    "<code>&lt;a href=\"https://t.me/name\"&gt;link&lt;/a&gt;</code>\n"
    "<code>&lt;blockquote&gt;quote&lt;/blockquote&gt;</code> (<code>&lt;blockquote expandable&gt;</code> for an "
    "expandable one) and <code>&lt;br&gt;</code> for a line break.\n"
    "Write <code>&amp;lt;</code> for a &lt; that must stay. A text that has an HTML tag with its closing tag is read "
    "as HTML only.\n\n"
    "<b>Example</b>\n"
    "<pre>**Attack on Titan** - Episode 12\n"
    "__Hindi__ | ||spoiler||\n"
    "[Watch here](https://t.me/yourchannel)</pre>"
)
