"""Telegram rich messages: headings, lists, tables, details, math, maps, media blocks, buttons ...

A rich message is not text plus entities: it is a tree of blocks. Telegram builds that tree itself from HTML or Markdown
(InputRichMessageHTML / InputRichMessageMarkdown), so text typed in those languages is handed over unchanged - every block
and every formatting Telegram knows is supported without this bot having to know it. A rich message that is sent to the
bot natively (or forwarded to it) is kept block for block (InputRichMessage).

How the bot keeps one: as a single entry in the post's `entities` list - ``{"k": "rich", "f": "html" | "markdown" |
"blocks", "s": source, ...}`` - next to a plain-text version in `text` (for lists, panels and the like). Whatever sends or
edits a post looks for that entry with `rich_of`. No database change is needed, and exports, backups and the undo log carry
it like any other formatting.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import List, Optional

from telethon import errors, functions, types, utils

from .tgutil import _b64, _unb64

log = logging.getLogger(__name__)

KIND = "rich"  # the "k" of the entry in a post's entities
MAX_SOURCE = 32768  # characters Telegram allows in a rich message (500 blocks, 16 levels and 50 media as well)


class RichError(ValueError):
    """A rich message that can't be used; the text is safe to show to the owner."""


@dataclass(frozen=True)
class Rich:
    """A rich message as the bot keeps it."""

    fmt: str  # "html" | "markdown" (typed: Telegram reads it) | "blocks" (received: base64 of an InputRichMessage)
    source: str
    rtl: bool = False
    noautolink: bool = False  # links, @mentions, #tags ... in the text are not made clickable by Telegram
    fp: Optional[str] = None  # fingerprint of the blocks Telegram showed last time (what the sync compares)


def entry(r: Rich) -> dict:
    """The entry that stands for `r` in a post's entities."""
    e: dict = {"k": KIND, "f": r.fmt, "s": r.source}
    if r.rtl:
        e["r"] = 1
    if r.noautolink:
        e["n"] = 1
    if r.fp:
        e["p"] = r.fp
    return e


def rich_of(obj) -> Optional[Rich]:
    """The rich message a post (or anything with a post's `entities`) holds, or None."""
    for d in getattr(obj, "entities", None) or []:
        if isinstance(d, dict) and d.get("k") == KIND and d.get("f") in ("html", "markdown", "blocks") and d.get("s"):
            return Rich(d["f"], d["s"], bool(d.get("r")), bool(d.get("n")), d.get("p") or None)
    return None


def is_rich(obj) -> bool:
    return rich_of(obj) is not None


def real_entities(entities) -> list:
    """The formatting entries of a stored entities list, without the rich message's."""
    return [d for d in entities or [] if not (isinstance(d, dict) and d.get("k") == KIND)]


# ------------------------------------------------------------------------------------ what Telegram takes
def to_input(r: Rich):
    """The InputRichMessage* object to send for `r`."""
    flags = dict(rtl=True if r.rtl else None, noautolink=True if r.noautolink else None)
    if r.fmt == "html":
        return types.InputRichMessageHTML(html=r.source, **flags)
    if r.fmt == "markdown":
        return types.InputRichMessageMarkdown(markdown=r.source, **flags)
    return _unb64(r.source)


def blank_references(inp):
    """The same InputRichMessage with the file references of its photos and files left out (they may have expired)."""
    if not isinstance(inp, types.InputRichMessage):
        return inp
    out = copy.copy(inp)
    out.photos = [types.InputPhoto(p.id, p.access_hash, b"") for p in inp.photos or []] or None
    out.documents = [types.InputDocument(d.id, d.access_hash, b"") for d in inp.documents or []] or None
    return out


async def with_rich(r: Rich, call):
    """call(InputRichMessage*): first as saved, then (if Telegram says a saved file reference expired) without them."""
    try:
        return await call(to_input(r))
    except errors.RPCError as e:
        if r.fmt != "blocks" or "FileReference" not in type(e).__name__:
            raise
        log.info("stored file reference of a rich message rejected (%s) - retrying without it", type(e).__name__)
        return await call(blank_references(to_input(r)))


def sent_message(result):
    """The message a send request created (something with an `id`)."""
    for u in getattr(result, "updates", None) or []:
        if isinstance(u, (types.UpdateNewChannelMessage, types.UpdateNewMessage)):
            return u.message
    if getattr(result, "id", None) is not None and not isinstance(result, (list, tuple)):
        return SimpleNamespace(id=result.id)  # UpdateShortSentMessage
    raise RuntimeError("Telegram did not say which message it created")


async def send_rich(client, peer, r: Rich, *, markup=None, silent=None):
    """Send `r` as a new message to `peer`; the message that was created comes back."""
    result = await with_rich(
        r,
        lambda inp: client(
            functions.messages.SendMessageRequest(
                peer=peer, message="", rich_message=inp, reply_markup=markup, silent=True if silent else None
            )
        ),
    )
    return sent_message(result)


# --------------------------------------------------------------------------------- rich messages that are received
def is_rich_message(m) -> bool:
    """The message carries a rich message (blocks) instead of plain text."""
    return getattr(m, "rich_message", None) is not None


def fingerprint(rm) -> str:
    """Identifies what a rich message shows (its blocks; the file references of its pictures change from fetch to fetch)."""
    h = hashlib.sha1()
    for b in getattr(rm, "blocks", None) or []:
        h.update(bytes(b))
    h.update(b"r" if getattr(rm, "rtl", None) else b"-")
    return h.hexdigest()[:20]


def _input_files(items, getter, empty) -> list:
    out = []
    for item in items or []:
        try:
            inp = getter(item)
        except (TypeError, ValueError):
            continue
        if not isinstance(inp, empty):
            out.append(inp)
    return out


def input_of(rm, users: Optional[list] = None):
    """The InputRichMessage that sends a received RichMessage again: same blocks, same pictures and files."""
    photos = _input_files(getattr(rm, "photos", None), utils.get_input_photo, types.InputPhotoEmpty)
    docs = _input_files(getattr(rm, "documents", None), utils.get_input_document, types.InputDocumentEmpty)
    return types.InputRichMessage(
        blocks=list(rm.blocks),
        rtl=True if getattr(rm, "rtl", None) else None,
        photos=photos or None,
        documents=docs or None,
        users=users or None,
    )


def rich_from_received(rm, users: Optional[list] = None) -> Rich:
    return Rich("blocks", _b64(input_of(rm, users)), bool(getattr(rm, "rtl", None)), False, fingerprint(rm))


def mention_ids(blocks) -> list:
    """User ids that the blocks mention by link (a text mention needs the account's access data to be sent again)."""
    found: list = []

    def walk(o) -> None:
        if isinstance(o, dict):
            if o.get("_") == "TextMentionName" and isinstance(o.get("user_id"), int):
                found.append(o["user_id"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for b in blocks or []:
        walk(b.to_dict())
    return list(dict.fromkeys(found))


async def input_users(client, ids: list) -> list:
    out = []
    for uid in ids:
        try:
            peer = await client.get_input_entity(uid)
        except Exception as e:  # noqa: BLE001 - a mention that can't be resolved is sent as the plain name
            log.info("could not resolve the mentioned user %s: %s", uid, type(e).__name__)
            continue
        if isinstance(peer, types.InputPeerUser):
            out.append(types.InputUser(peer.user_id, peer.access_hash))
    return out


async def received(client, event) -> Optional[Rich]:
    """The rich message that `event`'s message is (sent natively or forwarded to the bot), or None for an ordinary message."""
    m = event.message
    rm = getattr(m, "rich_message", None)
    if rm is None:
        return None
    if getattr(rm, "part", None):  # Telegram sent a part of it; the rest has to be asked for
        full = None
        try:
            got = await client(functions.messages.GetRichMessageRequest(peer=await event.get_input_chat(), id=m.id))
            full = got if isinstance(got, types.RichMessage) else getattr(got, "rich_message", None)
        except errors.RPCError as e:
            log.info("could not read the whole rich message: %s", type(e).__name__)
        if full is None or getattr(full, "part", None):
            raise RichError("Telegram only handed me a part of that rich message. Send it again, or type it as HTML / Markdown.")
        rm = full
    if not getattr(rm, "blocks", None):
        raise RichError("That rich message has no content.")
    return rich_from_received(rm, await input_users(client, mention_ids(rm.blocks)))


# ------------------------------------------------------------------------- plain text (lists, panels, previews)
_BLOCKY = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "table", "tr", "details", "summary", "figure",
           "figcaption", "aside", "blockquote", "pre", "footer", "hr", "br", "tg-math-block", "tg-button-row", "tg-collage",
           "tg-slideshow", "tg-map", "img", "video", "audio", "tg-document", "caption", "cite")


class _Plain(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.skip = 0

    def _break(self, tag: str) -> None:
        """A line break for a block; two blocks in a row are only one line apart (a <br> is always a break)."""
        if tag == "br" or (self.out and not self.out[-1].endswith("\n")):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in _BLOCKY:
            self._break(tag)
        if tag == "li":
            self.out.append("• ")
        elif tag in ("td", "th"):
            self.out.append(" | " if self.out and not self.out[-1].endswith(("\n", " | ")) else "")
        elif tag == "input" and any(k == "type" and v == "checkbox" for k, v in attrs):
            self.out.append("☑ " if any(k == "checked" for k, _ in attrs) else "☐ ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in _BLOCKY:
            self._break(tag)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _plain_html(source: str) -> str:
    p = _Plain()
    try:
        p.feed(source)
        p.close()
    except Exception:  # noqa: BLE001 - a snippet is not worth failing for
        return _tidy(re.sub(r"<[^>]*>", " ", source))
    return _tidy("".join(p.out))


_MD_EMPHASIS = re.compile(r"(\*\*|__|~~|\|\||==)(?=\S)(.+?)(?<=\S)\1")
_MD_SINGLE = re.compile(r"(?<![\w*])([*_])(?=\S)([^*_\n]+?)(?<=\S)\1(?![\w*])")


def _plain_markdown(source: str) -> str:
    out: List[str] = []
    fenced = False
    for line in source.replace("\r", "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if fenced:
            out.append(line)
            continue
        if re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$", line):  # a table's delimiter row
            continue
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            out.append("")
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^(\s*)(?:>\s?)+", r"\1", line)
        line = re.sub(r"^(\s*)[-*+]\s+\[[xX]\]\s+", r"\1☑ ", line)
        line = re.sub(r"^(\s*)[-*+]\s+\[ \]\s+", r"\1☐ ", line)
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        if "|" in line and line.count("|") >= 2:
            line = " | ".join(c.strip() for c in line.strip().strip("|").split("|"))
        line = re.sub(r"!\[([^\]]*)\]\([^)]*?(?:\s+\"([^\"]*)\")?\)", lambda m: m.group(2) or m.group(1), line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"^\[\^([^\]\s]+)\]:[ \t]*", r"\1: ", line)  # a footnote's text, led by its mark
        line = re.sub(r"\[\^[^\]]+\]", "", line)
        line = _MD_EMPHASIS.sub(r"\2", line)
        line = _MD_SINGLE.sub(r"\2", line)
        line = line.replace("`", "")
        line = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~>$=])", r"\1", line)
        out.append(line)
    return _tidy("\n".join(out))


def _plain_blocks(blocks) -> str:
    out: List[str] = []

    def brk() -> None:
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    def walk(o) -> None:
        if isinstance(o, dict):
            name = str(o.get("_", ""))
            if name == "PageTableRow":
                brk()
                for n, cell in enumerate(o.get("cells") or []):
                    if n:
                        out.append(" | ")
                    walk(cell)
                brk()
                return
            item = name.startswith(("PageListItem", "PageListOrderedItem"))
            if name.startswith("PageBlock") or item:
                brk()
            if item:
                out.append(("☑ " if o.get("checked") else "☐ ") if o.get("checkbox") else "• ")
            for key in sorted(o, key=lambda k: k != "title"):  # a title (of a <details>, a table) is read first
                v = o[key]
                if key in ("text", "source") and isinstance(v, str):
                    out.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
            if name.startswith("PageBlock") or item:
                brk()
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for b in blocks or []:
        try:
            walk(b.to_dict())
        except Exception:  # noqa: BLE001
            continue
    return _tidy("".join(out))


def plain_of(r: Rich) -> str:
    """What the rich message says, as plain text (what My posts shows and searches)."""
    if r.fmt == "html":
        return _plain_html(r.source)
    if r.fmt == "markdown":
        return _plain_markdown(r.source)
    try:
        inp = _unb64(r.source)
        return _plain_blocks(getattr(inp, "blocks", None))
    except Exception:  # noqa: BLE001
        return ""


def plain_of_message(m) -> str:
    return _plain_blocks(getattr(getattr(m, "rich_message", None), "blocks", None))


# ---------------------------------------------------------------------------- reading what the owner typed
_FORCE = re.compile(r"\A[ \t]*<rich((?:[ \t]+(?:rtl|noautolink))*)[ \t]*>[ \t]*(?:\r?\n|\Z)", re.I)

# HTML only rich messages have: headings, paragraphs, lists, tables, details, figures, media, maps, math, buttons, marks ...
_RICH_TAGS = (
    r"h[1-6]", "p", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "details", "summary",
    "figure", "figcaption", "aside", "hr", "img", "video", "audio", "footer", "cite", "input", "mark", "sub", "sup",
    "tg-document", "tg-map", "tg-collage", "tg-slideshow", "tg-math", "tg-math-block", "tg-button", "tg-button-row",
    "tg-time", "tg-reference",
)
_RICH_HTML = re.compile(
    r"<(?:" + "|".join(t if t.startswith("h[") else re.escape(t) for t in _RICH_TAGS) + r")(?=[\s/>])[^<>]*>"
    r"|<a\s[^<>]*?\b(?:name\s*=|href\s*=\s*[\"']?#)",
    re.I,
)
_BASIC_HTML = re.compile(r"<(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|blockquote|tg-spoiler|tg-emoji)(?=[\s/>])[^<>]*>", re.I)

# Markdown only rich messages have (a bullet list or a rule alone does not count: plain posts use those as decoration)
_MD_RICH = (
    re.compile(r"^ {0,3}#{1,6}[ \t]+\S", re.M),  # # heading
    re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\[[ xX]\](?:[ \t]+\S|[ \t]*$)", re.M),  # - [x] task
    re.compile(r"^[^\n]*\|[^\n]*\n[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$", re.M),  # | table |
    re.compile(r"\$\$.+?\$\$", re.S),  # $$ block math $$
    re.compile(r"^\[\^[^\]\s]+\]:[ \t]+\S", re.M),  # [^1]: footnote
    re.compile(r"(?<![=\w])==(?=\S)[^=\n]+?(?<=\S)==(?![=\w])"),  # ==marked==
    re.compile(r"^[ \t]*!\[[^\]\n]*\]\((?!tg://(?:emoji|time)\b)[^)\s]+(?:[ \t]+\"[^\"\n]*\")?\)[ \t]*$", re.M),  # ![](picture)
    re.compile(r"!\[[^\]\n]*\]\(tg://time\?[^)\s]+\)"),  # a formatted date
)
_CODE = re.compile(r"```.*?```|~~~.*?~~~|<pre\b.*?</pre\s*>|<code\b.*?</code\s*>|`[^`\n]+`", re.S | re.I)


def _blank(m: "re.Match") -> str:
    return re.sub(r"[^\n]", " ", m.group())


def detect(text: str, forced: bool = False) -> Optional[str]:
    """"html" or "markdown" when `text` needs a rich message (or `forced` says it is one), else None."""
    bare = _CODE.sub(_blank, text)  # what stands in code is not markup
    html_hit = bool(_RICH_HTML.search(bare))
    if html_hit and bare.lstrip().startswith("<"):
        return "html"
    if any(rx.search(bare) for rx in _MD_RICH):
        return "markdown"
    if html_hit:
        return "html"
    if forced:
        return "html" if _BASIC_HTML.search(bare) else "markdown"
    return None


def read_typed(text: str) -> Optional[Rich]:
    """The rich message that text typed as HTML or Markdown stands for, or None when it is an ordinary text.

    It is a rich message when it has something only rich messages have (a heading, a table, a task list, math, a picture,
    <details> ...), or when its first line is ``<rich>`` (``<rich rtl>``, ``<rich noautolink>``). Telegram reads the
    language itself; what is passed on is exactly what was typed (without that first line)."""
    text = text or ""
    force = _FORCE.match(text)
    body = text[force.end():] if force else text
    fmt = detect(body, forced=bool(force))
    if fmt is None or not body.strip():
        return None
    words = (force.group(1) or "").lower().split() if force else []
    return Rich(fmt, body.strip(), rtl="rtl" in words, noautolink="noautolink" in words)


def check(r: Rich) -> None:
    """RichError when Telegram would not take it (only the limit that is cheap to check here)."""
    if r.fmt != "blocks" and len(r.source) > MAX_SOURCE:
        raise RichError(f"A rich message can have at most {MAX_SOURCE} characters (yours has {len(r.source)}).")


def with_fingerprint(r: Rich, fp: Optional[str]) -> Rich:
    return replace(r, fp=fp)


# --------------------------------------------------------------------------------------- the /rich help pages
RICH_HELP = (
    "🧩 <b>Rich messages</b>\n\n"
    "Telegram's newer message type: headings, lists, tables, expandable sections, math, maps, pictures and buttons in one "
    "message (up to 32768 characters, 500 blocks, 16 nesting levels, 50 pictures/files, 20 table columns). The bot "
    "publishes your text as one when\n"
    "• you send it a rich message made in Telegram, or forward one: it is kept block for block, or\n"
    "• you type HTML or Markdown that has something only rich messages have: a heading, a table, a task list, math, a "
    "picture, <code>&lt;details&gt;</code>, <code>&lt;h1&gt;</code>, <code>&lt;table&gt;</code> ... or\n"
    "• the first line is just <code>&lt;rich&gt;</code> (<code>&lt;rich rtl&gt;</code> for right-to-left text, "
    "<code>&lt;rich noautolink&gt;</code> so links, @names and #tags are not made clickable).\n\n"
    "Telegram reads the text itself, so everything it supports works. 👁 Preview shows what it made of your text before "
    "you publish. Rich messages are for text posts - a photo's caption can't be rich. The post's own link buttons "
    "(➕ Button) work with them.\n\n"
    "<b>Markdown</b>\n"
    "<code>**bold** *italic* ~~strike~~ ||spoiler|| ==marked== `code`</code> (underline, subscript and superscript: HTML "
    "<code>&lt;u&gt; &lt;sub&gt; &lt;sup&gt;</code>)\n"
    "<code># Heading</code> … <code>###### Heading</code>\n"
    "<code>- item</code> <code>1. item</code> lists, <code>- [ ] task</code> <code>- [x] done</code>, <code>---</code> a rule\n"
    "<code>&gt; quote</code> (a bare <code>&gt;</code> line is a new paragraph inside it)\n"
    "<code>```python</code> … <code>```</code> a code block with a language\n"
    "<code>$x^2$</code> inline math, <code>$$E = mc^2$$</code> or a <code>```math</code> block: a formula block\n"
    "<code>| a | b |</code> then <code>|:--|--:|</code> then rows: a table (colons set the alignment; cells hold inline "
    "formatting only)\n"
    "<code>text[^1]</code> and a line <code>[^1]: the note</code>: a footnote\n"
    "<code>![](https://…/pic.jpg \"caption\")</code> alone on a line: a picture, video, audio, GIF or file (https links "
    "only); put several such lines inside <code>&lt;tg-collage&gt;</code> or <code>&lt;tg-slideshow&gt;</code>\n"
    "<code>![22:45 tomorrow](tg://time?unix=1647531900&amp;format=wDT)</code> a date shown in the reader's time\n"
    "<code>![](tg://emoji?id=5368324170671202286)</code> a custom emoji\n"
    "<code>[name](tg://user?id=123)</code> <code>[mail](mailto:a@b.c)</code> <code>[call](tel:+123)</code>\n"
    "Raw HTML works inside Markdown, but Markdown is not read inside block tags (only inside <code>&lt;details&gt;</code>, "
    "<code>&lt;tg-collage&gt;</code> and <code>&lt;tg-slideshow&gt;</code>)."
)

RICH_HELP_HTML = (
    "🧩 <b>Rich HTML</b>\n\n"
    "<code>&lt;h1&gt;</code> … <code>&lt;h6&gt;</code> <code>&lt;p&gt;</code> <code>&lt;hr&gt;</code> "
    "<code>&lt;footer&gt;</code> <code>&lt;b&gt; &lt;i&gt; &lt;u&gt; &lt;s&gt; &lt;mark&gt; &lt;sub&gt; &lt;sup&gt; "
    "&lt;code&gt; &lt;tg-spoiler&gt;</code> <code>&lt;br&gt;</code>\n"
    "<code>&lt;pre&gt;&lt;code class=\"language-python\"&gt;</code> a code block\n"
    "<code>&lt;ul&gt;</code> <code>&lt;ol start=\"3\" type=\"a\" reversed&gt;</code> "
    "<code>&lt;li value=\"7\" type=\"i\"&gt;</code> (type: a, A, i, I, 1) "
    "<code>&lt;input type=\"checkbox\" checked&gt;</code>\n"
    "<code>&lt;blockquote expandable&gt;text&lt;cite&gt;who&lt;/cite&gt;&lt;/blockquote&gt;</code> and "
    "<code>&lt;aside&gt;pull quote&lt;cite&gt;who&lt;/cite&gt;&lt;/aside&gt;</code>\n"
    "<code>&lt;details open&gt;&lt;summary&gt;Title&lt;/summary&gt;…&lt;/details&gt;</code> an expandable section\n"
    "<code>&lt;table bordered striped compact&gt;&lt;caption&gt;…&lt;/caption&gt;&lt;tr&gt;&lt;th colspan=\"2\" "
    "align=\"center\" valign=\"middle\"&gt;…</code> (td and th: colspan, rowspan, align left/center/right, valign "
    "top/middle/bottom)\n"
    "<code>&lt;tg-math&gt;x^2&lt;/tg-math&gt;</code> <code>&lt;tg-math-block&gt;E = mc^2&lt;/tg-math-block&gt;</code> "
    "(raw LaTeX)\n"
    "<code>&lt;tg-time unix=\"1647531900\" format=\"wDT\"&gt;22:45 tomorrow&lt;/tg-time&gt;</code>\n"
    "<code>&lt;tg-emoji emoji-id=\"5368324170671202286\"&gt;🙂&lt;/tg-emoji&gt;</code>\n"
    "<code>&lt;a name=\"top\"&gt;&lt;/a&gt;</code> and <code>&lt;a href=\"#top\"&gt;</code> anchors; "
    "<code>&lt;tg-reference name=\"n1\"&gt;text&lt;/tg-reference&gt;</code> with <code>&lt;a href=\"#n1\"&gt;</code> "
    "a footnote\n"
    "<code>&lt;img src=\"https://…\"&gt;</code> <code>&lt;video src=…&gt;</code> <code>&lt;audio src=…&gt;</code> "
    "<code>&lt;tg-document src=…&gt;</code>, each on its own or in "
    "<code>&lt;figure&gt;…&lt;figcaption&gt;caption&lt;cite&gt;credit&lt;/cite&gt;&lt;/figcaption&gt;&lt;/figure&gt;</code> "
    "(https links only; <code>tg-spoiler</code> on img/video hides it); several inside "
    "<code>&lt;tg-collage&gt;</code> or <code>&lt;tg-slideshow&gt;</code>\n"
    "<code>&lt;tg-map lat=\"41.9\" long=\"12.5\" zoom=\"14\"/&gt;</code> a map\n"
    "<code>&lt;tg-button-row align=\"center\"&gt;&lt;tg-button type=\"url\" url=\"https://t.me\" "
    "style=\"success\"&gt;Open&lt;/tg-button&gt;&lt;/tg-button-row&gt;</code> buttons in the text. Types: url, "
    "web_app and login_url (url=), callback_data (data=), copy_text (text=), switch_inline_query, "
    "switch_inline_query_current_chat, switch_inline_query_chosen_chat (query=), disabled. Styles: primary, success, "
    "danger, link.\n"
    "<code>&amp;lt; &amp;gt; &amp;amp;</code> stand for &lt; &gt; &amp; when they must stay as they are.\n\n"
    "<b>Good to know</b>\n"
    "• A picture or video typed into a rich message must be a public https link. To use a file that exists only on "
    "Telegram, send the bot a rich message that already contains it.\n"
    "• A button inside the text that needs the bot (callback_data) only works for the bot that made it - for a forwarded "
    "rich message that is another bot.\n"
    "• /swap, /handleswap and the post-link changes of /repost change a rich post's buttons, not the links inside its text.\n"
    "• Older Telegram apps may not show rich messages."
)
