"""Rich messages (headings, lists, tables, <details>, math, maps, pictures, buttons ...): what is recognised, how a rich message is
kept in a post, what is sent to Telegram for it and how one that is sent to the bot is read."""
import asyncio
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from app.richmsg import (
    RICH_HELP,
    RICH_HELP_HTML,
    MAX_SOURCE,
    Rich,
    RichError,
    blank_references,
    check,
    detect,
    entry,
    fingerprint,
    input_users,
    is_rich,
    is_rich_message,
    mention_ids,
    plain_of,
    plain_of_message,
    read_typed,
    real_entities,
    received,
    rich_from_received,
    rich_of,
    send_rich,
    sent_message,
    to_input,
    with_rich,
)
from app.tgutil import de_entities, edit_raw

from .fakes import photo_media, rpc, video_media

T = types.TextPlain


def blocks(word="Hello"):
    return [types.PageBlockHeading1(T("Title")), types.PageBlockParagraph(types.TextBold(T(word)))]


def rich_msg(bl=None, *, photos=None, documents=None, rtl=None, part=None):
    """A rich message as Telegram hands it to the bot."""
    return types.RichMessage(blocks=bl or blocks(), photos=photos or [], documents=documents or [], rtl=rtl, part=part)


def same(a, b):
    """The same TL objects (they have no usable ==)."""
    return [bytes(x) for x in a] == [bytes(x) for x in b]


# ============================================================================================ what is a rich message
HTML_RICH = [
    "<h1>Title</h1>",
    "<h4 id='x'>Small</h4>",
    "<p>one</p><p>two</p>",
    "<ul><li>a</li><li>b</li></ul>",
    "<ol start=\"3\"><li>a</li></ol>",
    "<table bordered><tr><td>a</td></tr></table>",
    "<details><summary>More</summary>text</details>",
    "<tg-map lat=\"41.9\" long=\"12.5\" zoom=\"14\"/>",
    "<tg-math-block>E = mc^2</tg-math-block>",
    "<aside>pull<cite>me</cite></aside>",
    "<hr>",
    "<footer>fine print</footer>",
    "<img src=\"https://x.org/a.jpg\">",
    "<figure><img src=\"https://x.org/a.jpg\"><figcaption>c</figcaption></figure>",
    "<tg-collage><img src=\"https://x.org/a.jpg\"></tg-collage>",
    "<tg-button-row><tg-button type=\"url\" url=\"https://t.me\">Open</tg-button></tg-button-row>",
    "text with <mark>marked</mark> words",
    "H<sub>2</sub>O and x<sup>2</sup>",
    "<input type=\"checkbox\" checked> done",
    "<tg-time unix=\"1647531900\" format=\"wDT\">soon</tg-time>",
    "<a name=\"top\"></a>go",
    "back to <a href=\"#top\">the top</a>",
    "<tg-reference name=\"n1\">note</tg-reference>",
]
MARKDOWN_RICH = [
    "# Title",
    "text\n\n### Sub heading",
    "###### Smallest",
    "- [ ] todo",
    "1. [x] done",
    "| a | b |\n|---|---|\n| 1 | 2 |",
    "a | b\n:-:|--:\n1 | 2",
    "$$E = mc^2$$",
    "a note[^1]\n\n[^1]: the note",
    "some ==marked== text",
    "![](https://x.org/a.jpg)",
    "![](https://x.org/a.mp4 \"caption\")",
    "see ![22:45 tomorrow](tg://time?unix=1647531900&format=wDT)",
]
NOT_RICH = [
    "Episode 12 is out",
    "#anime #naruto",
    "#1 hit",  # a hashtag with no space after the # is not a heading
    "Price: $5 and $6",
    "a == b",
    "1 == 1 and 2 == 2",
    "I <3 this",
    "- one\n- two",
    "* a\n* b",
    "---",
    "1) first\n2) second",
    "**bold** and __under__ and ~~strike~~ and ||spoiler|| and `code`",
    "<b>bold</b> <i>it</i> <u>u</u> <s>s</s> <a href=\"https://t.me\">x</a> <code>c</code> <tg-spoiler>s</tg-spoiler>",
    "<blockquote>quote</blockquote> and <pre>block</pre>",
    "<pre>&lt;h1&gt; stays text</pre>",
    "`<h1>` is a tag",
    "```\n# not a heading\n```",
    "~~~\n| a | b |\n|---|---|\n~~~",
    "a | b | c",
    "5 > 3 and 2 < 4",
    "@channel_name look at https://example.com/a#section",
    "[watch](https://t.me/x) and ![smile](tg://emoji?id=1)",
    "",
    "   ",
]


@pytest.mark.parametrize("text", HTML_RICH)
def test_html_with_rich_blocks_is_a_rich_message(text):
    assert detect(text) == "html"
    assert read_typed(text) == Rich("html", text)


@pytest.mark.parametrize("text", MARKDOWN_RICH)
def test_markdown_with_rich_blocks_is_a_rich_message(text):
    assert detect(text) == "markdown"
    assert read_typed(text) == Rich("markdown", text)


@pytest.mark.parametrize("text", NOT_RICH)
def test_ordinary_text_is_not_turned_into_a_rich_message(text):
    assert detect(text) is None
    assert read_typed(text) is None


def test_html_wins_when_the_text_starts_with_a_tag_and_markdown_otherwise():
    assert detect("<h1>Title</h1>\n\n- [ ] not a task here") == "html"
    assert detect("# Title\n\nsome <mark>marked</mark> html inside") == "markdown"
    assert detect("intro\n<h2>Sub</h2>") == "html"


def test_the_rich_marker_makes_any_text_a_rich_message():
    assert read_typed("<rich>\nhello **x**") == Rich("markdown", "hello **x**")
    assert read_typed("<rich>\n<b>x</b> and <i>y</i>") == Rich("html", "<b>x</b> and <i>y</i>")
    r = read_typed("<rich rtl noautolink>\nhello #tag @name")
    assert (r.fmt, r.source, r.rtl, r.noautolink) == ("markdown", "hello #tag @name", True, True)
    r = read_typed("  <RICH   rtl >\r\nمرحبا")  # spaces, any case and a Windows line end are fine
    assert (r.source, r.rtl, r.noautolink) == ("مرحبا", True, False)
    assert read_typed("<rich noautolink>\n# Title") == Rich("markdown", "# Title", noautolink=True)
    # the marker is a line of its own, at the start
    assert read_typed("<rich> hello") is None
    assert read_typed("hello <rich>\nx") is None
    assert read_typed("hello\n<rich>\nx") is None
    assert read_typed("<rich>") is None and read_typed("<rich>\n  \n") is None


def test_what_is_typed_is_handed_over_exactly():
    src = "# Title  \n\n  indented\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert read_typed("\n" + src + "\n\n").source == src.strip()
    assert read_typed(src).source == src
    assert read_typed("<rich>\n" + src).source == src


def test_code_is_not_markup():
    assert detect("```python\n# comment\nprint('<h1>')\n```") is None
    assert detect("use `# Heading` for a heading and <code><h1></code> in HTML") is None
    assert detect("```\n- [ ] x\n```\n# Real heading") == "markdown"


# ================================================================================================= keeping a rich message
def test_a_rich_message_is_one_entry_among_the_entities_of_a_post():
    r = Rich("html", "<h1>x</h1>", rtl=True, noautolink=True, fp="abc")
    e = entry(r)
    assert e == {"k": "rich", "f": "html", "s": "<h1>x</h1>", "r": 1, "n": 1, "p": "abc"}
    assert entry(Rich("markdown", "# x")) == {"k": "rich", "f": "markdown", "s": "# x"}
    post = SimpleNamespace(entities=[{"k": "bold", "o": 0, "l": 1}, e])
    assert rich_of(post) == r and is_rich(post)
    assert real_entities(post.entities) == [{"k": "bold", "o": 0, "l": 1}]
    assert real_entities(None) == []
    for bad in ({"k": "rich", "f": "pdf", "s": "x"}, {"k": "rich", "f": "html", "s": ""}, {"k": "bold", "o": 0, "l": 1}, "x"):
        assert rich_of(SimpleNamespace(entities=[bad])) is None
    assert rich_of(SimpleNamespace(entities=None)) is None and rich_of(SimpleNamespace()) is None
    assert not is_rich(SimpleNamespace(entities=[]))


def test_the_entry_is_not_formatting():
    assert de_entities([entry(Rich("markdown", "# x"))]) == []
    assert de_entities([entry(Rich("html", "<b>x</b>")), {"k": "bold", "o": 0, "l": 2}]) == [types.MessageEntityBold(0, 2)]


# ============================================================================================ what is sent to Telegram
def test_typed_text_goes_to_telegram_as_html_or_markdown_for_it_to_read():
    i = to_input(Rich("html", "<h1>x</h1>", rtl=True))
    assert isinstance(i, types.InputRichMessageHTML) and i.html == "<h1>x</h1>" and i.rtl is True and i.noautolink is None
    i = to_input(Rich("markdown", "# x", noautolink=True))
    assert isinstance(i, types.InputRichMessageMarkdown) and i.markdown == "# x" and i.noautolink is True and i.rtl is None
    bytes(to_input(Rich("markdown", "# x")))  # serialises


def test_a_rich_message_that_was_received_is_sent_again_block_for_block():
    rm = rich_msg(photos=[photo_media(5).photo], documents=[video_media(81).document], rtl=True)
    r = rich_from_received(rm)
    assert (r.fmt, r.rtl, r.fp) == ("blocks", True, fingerprint(rm))
    inp = to_input(r)
    assert isinstance(inp, types.InputRichMessage) and inp.rtl is True
    assert same(inp.blocks, rm.blocks)
    assert [(p.id, p.access_hash, p.file_reference) for p in inp.photos] == [(5, 35, b"ref5")]
    assert [(d.id, d.access_hash, d.file_reference) for d in inp.documents] == [(81, 81 * 7, b"fr81")]
    bytes(inp)
    plain = to_input(rich_from_received(rich_msg()))
    assert plain.photos is None and plain.documents is None and plain.users is None and not plain.rtl


def test_file_references_can_be_left_out():
    inp = to_input(rich_from_received(rich_msg(photos=[photo_media(5).photo], documents=[video_media(81).document])))
    blank = blank_references(inp)
    assert blank.photos[0].file_reference == b"" and (blank.photos[0].id, blank.photos[0].access_hash) == (5, 35)
    assert blank.documents[0].file_reference == b"" and blank.documents[0].id == 81
    assert inp.photos[0].file_reference == b"ref5"  # the original is untouched
    html = types.InputRichMessageHTML(html="x")
    assert blank_references(html) is html


def test_a_stale_file_reference_is_retried_without_it():
    async def run():
        r = rich_from_received(rich_msg(photos=[photo_media(5).photo]))
        seen = []

        async def call(inp):
            seen.append(inp)
            if len(seen) == 1:
                raise rpc("FileReferenceExpiredError")
            return "ok"

        assert await with_rich(r, call) == "ok"
        assert seen[0].photos[0].file_reference == b"ref5" and seen[1].photos[0].file_reference == b""

        async def always(inp):
            raise rpc("FileReferenceExpiredError")

        with pytest.raises(errors.RPCError):
            await with_rich(r, always)  # the second try failed as well: that error is shown
        with pytest.raises(errors.RPCError):
            await with_rich(Rich("html", "<p>x</p>"), always)  # typed text has no references to blame

        async def other(inp):
            raise rpc("MessageEmptyError")

        with pytest.raises(errors.MessageEmptyError):
            await with_rich(r, other)

    asyncio.run(run())


def test_send_rich_makes_one_raw_request_and_finds_the_message_it_created():
    async def run():
        sent = []

        async def client(req):
            sent.append(req)
            return types.Updates(
                updates=[types.UpdateNewChannelMessage(message=types.Message(id=41, peer_id=types.PeerChannel(1), date=None, message=""), pts=1, pts_count=1)],
                users=[], chats=[], date=None, seq=0,
            )

        markup = types.ReplyInlineMarkup([])
        m = await send_rich(client, types.InputPeerChannel(1, 5), Rich("markdown", "# x"), markup=markup, silent=True)
        assert m.id == 41
        (req,) = sent
        assert isinstance(req, functions.messages.SendMessageRequest)
        assert req.message == "" and req.reply_markup is markup and req.silent is True
        assert isinstance(req.rich_message, types.InputRichMessageMarkdown) and req.rich_message.markdown == "# x"
        bytes(req)

    asyncio.run(run())


def test_the_message_a_send_created_is_found_in_every_answer_telegram_gives():
    short = types.UpdateShortSentMessage(id=7, pts=1, pts_count=1, date=None, out=True)
    assert sent_message(short).id == 7
    new = types.UpdateNewMessage(message=types.Message(id=8, peer_id=types.PeerUser(1), date=None, message=""), pts=1, pts_count=1)
    assert sent_message(types.Updates(updates=[new], users=[], chats=[], date=None, seq=0)).id == 8
    with pytest.raises(RuntimeError):
        sent_message(types.Updates(updates=[], users=[], chats=[], date=None, seq=0))


def test_edit_raw_makes_the_message_that_rich_message():
    async def run():
        sent = []

        async def client(req):
            sent.append(req)

        inp = types.InputRichMessageMarkdown(markdown="# x")
        peer = types.InputPeerChannel(1, 5)
        await edit_raw(client, peer, 9, rich_message=inp, markup=None, text="ignored", entities=[types.MessageEntityBold(0, 1)], preview=True)
        (req,) = sent
        assert req.rich_message is inp and req.message is None and not req.entities and not req.no_webpage and req.reply_markup is None
        bytes(req)
        sent.clear()
        await edit_raw(client, peer, 9, text="plain", entities=[], markup=None)
        assert sent[0].rich_message is None and sent[0].message == "plain"

    asyncio.run(run())


def test_a_rich_message_longer_than_telegram_allows_is_refused():
    check(Rich("html", "x" * MAX_SOURCE))
    check(Rich("blocks", "x" * (MAX_SOURCE * 2)))  # received blocks are Telegram's own: nothing to count here
    with pytest.raises(RichError) as exc:
        check(Rich("markdown", "x" * (MAX_SOURCE + 1)))
    assert "32768" in str(exc.value)


# ===================================================================================== a rich message sent to the bot
def test_the_fingerprint_follows_what_the_message_shows():
    a, b = rich_msg(blocks("x")), rich_msg(blocks("x"), photos=[photo_media(5).photo])
    assert fingerprint(a) == fingerprint(b)  # the pictures' file references change from fetch to fetch
    assert fingerprint(a) != fingerprint(rich_msg(blocks("y")))
    assert fingerprint(a) != fingerprint(rich_msg(blocks("x"), rtl=True))
    assert len(fingerprint(a)) == 20


def test_is_rich_message():
    assert is_rich_message(SimpleNamespace(rich_message=rich_msg()))
    assert not is_rich_message(SimpleNamespace(rich_message=None)) and not is_rich_message(SimpleNamespace())


def test_people_mentioned_by_link_are_found_and_looked_up():
    async def run():
        bl = [types.PageBlockParagraph(types.TextConcat([T("hi "), types.TextMentionName(T("Bob"), 42), types.TextMentionName(T("Bob"), 42), types.TextMentionName(T("Al"), 43)]))]
        assert mention_ids(bl) == [42, 43]
        assert mention_ids(blocks()) == [] and mention_ids(None) == []

        class Client:
            async def get_input_entity(self, uid):
                if uid == 42:
                    return types.InputPeerUser(42, 777)
                raise ValueError("unknown")

        users = await input_users(Client(), [42, 43])
        assert [(u.user_id, u.access_hash) for u in users] == [(42, 777)]  # the one that can't be found is left out
        r = rich_from_received(rich_msg(bl), users)
        assert [u.user_id for u in to_input(r).users] == [42]

    asyncio.run(run())


class Event:
    """The message event of a rich message that arrived (what `received` reads)."""

    def __init__(self, rm, mid=77):
        self.message = SimpleNamespace(rich_message=rm, id=mid)

    async def get_input_chat(self):
        return types.InputPeerUser(1, 0)


class Client:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.requests = answer, error, []

    async def __call__(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        return self.answer

    async def get_input_entity(self, uid):
        raise ValueError(uid)


def test_a_whole_rich_message_is_read_as_it_arrived():
    async def run():
        rm = rich_msg(photos=[photo_media(5).photo])
        client = Client()
        r = await received(client, Event(rm))
        assert client.requests == []  # nothing more to ask
        assert (r.fmt, r.fp) == ("blocks", fingerprint(rm)) and plain_of(r) == "Title\nHello"
        assert await received(client, Event(None)) is None  # an ordinary message

    asyncio.run(run())


def test_a_rich_message_that_arrives_in_parts_is_fetched_whole():
    async def run():
        part = rich_msg([types.PageBlockParagraph(T("first"))], part=True)
        client = Client(answer=rich_msg(blocks("whole")))
        r = await received(client, Event(part, mid=9))
        (req,) = client.requests
        assert isinstance(req, functions.messages.GetRichMessageRequest) and req.id == 9
        assert plain_of(r) == "Title\nwhole"

    asyncio.run(run())


def test_a_rich_message_that_cannot_be_had_whole_is_refused_in_plain_words():
    async def run():
        part = rich_msg([types.PageBlockParagraph(T("first"))], part=True)
        for client in (
            Client(answer=rich_msg(blocks(), part=True)),  # still only a part
            Client(error=rpc("MessageIdInvalidError")),  # Telegram refuses to say more
            Client(answer=None),  # nothing came back
        ):
            with pytest.raises(RichError) as exc:
                await received(client, Event(part))
            assert "part of that rich message" in str(exc.value)
        with pytest.raises(RichError) as exc:
            await received(Client(), Event(types.RichMessage(blocks=[], photos=[], documents=[])))
        assert "no content" in str(exc.value)

    asyncio.run(run())


# ==================================================================================== the plain text of a rich message
def test_the_plain_text_of_typed_html():
    html = (
        "<h1>Schedule</h1><p>Every <b>Monday</b></p><ul><li>one</li><li><input type=\"checkbox\" checked> two</li></ul>"
        "<table><tr><th>Day</th><th>Show</th></tr><tr><td>Mon</td><td>Naruto</td></tr></table>"
        "<details><summary>More</summary>hidden text</details><blockquote>quote<cite>me</cite></blockquote>"
        "<tg-math-block>E = mc^2</tg-math-block>"
    )
    assert plain_of(Rich("html", html)) == (
        "Schedule\nEvery Monday\n• one\n• ☑ two\nDay | Show\nMon | Naruto\nMore\nhidden text\nquote\nme\nE = mc^2"
    )
    assert plain_of(Rich("html", "line one<br>line two<br><br>after&nbsp;a gap &amp; more")) == "line one\nline two\n\nafter a gap & more"
    assert plain_of(Rich("html", "a<script>var x = 1;</script>b")) == "ab"


def test_the_plain_text_of_typed_markdown():
    md = (
        "# Title\n\nSome **bold**, *it* and ==mark== [link](https://t.me/x)\n\n- [x] done\n- [ ] todo\n- item\n\n"
        "| a | b |\n|:--|--:|\n| 1 | 2 |\n\n> quoted\n\n```python\nprint(1)\n```\n\n$$E = mc^2$$\n\n"
        "note[^1]\n\n[^1]: the note\n\n![](https://x.org/a.jpg \"a caption\")\n\n---\n\nend"
    )
    assert plain_of(Rich("markdown", md)) == (
        "Title\n\nSome bold, it and mark link\n\n☑ done\n☐ todo\n• item\n\na | b\n1 | 2\n\nquoted\n\nprint(1)\n\n"
        "$$E = mc^2$$\n\nnote\n\n1: the note\n\na caption\n\nend"
    )


def test_the_plain_text_of_a_rich_message_made_in_telegram():
    bl = [
        types.PageBlockHeading1(T("Title")),
        types.PageBlockParagraph(types.TextConcat([T("Every "), types.TextBold(T("Monday"))])),
        types.PageBlockList([types.PageListItemText(T("one")), types.PageListItemText(T("two"), checkbox=True, checked=True)]),
        types.PageBlockTable(
            T("sched"),
            [
                types.PageTableRow([types.PageTableCell(text=T("Day"), header=True), types.PageTableCell(text=T("Show"), header=True)]),
                types.PageTableRow([types.PageTableCell(text=T("Mon")), types.PageTableCell(text=T("Naruto"))]),
            ],
        ),
        types.PageBlockMath("E = mc^2"),
        types.PageBlockDetails([types.PageBlockParagraph(T("hidden"))], T("More")),
    ]
    want = "Title\nEvery Monday\n• one\n☑ two\nsched\nDay | Show\nMon | Naruto\nE = mc^2\nMore\nhidden"
    rm = rich_msg(bl)
    assert plain_of(rich_from_received(rm)) == want
    assert plain_of_message(SimpleNamespace(rich_message=rm)) == want
    assert plain_of_message(SimpleNamespace(rich_message=None)) == ""
    assert plain_of(Rich("blocks", "not base64 of anything")) == ""


# ============================================================================================================== the help
def test_the_help_pages_fit_in_a_message_and_cover_the_blocks():
    for page in (RICH_HELP, RICH_HELP_HTML):
        assert len(page) < 4096
    help_all = RICH_HELP + RICH_HELP_HTML
    for word in (
        "&lt;rich&gt;", "# Heading", "- [ ] task", "```python", "$$E = mc^2$$", "[^1]", "tg://time", "tg://emoji", "==marked==",
        "&lt;details", "&lt;table", "&lt;tg-math-block&gt;", "&lt;tg-map", "&lt;tg-collage&gt;", "&lt;tg-slideshow&gt;",
        "&lt;tg-button-row", "&lt;aside&gt;", "&lt;figure&gt;", "&lt;tg-reference", "&lt;tg-time", "&lt;sub&gt;", "https",
    ):
        assert word in help_all, word
