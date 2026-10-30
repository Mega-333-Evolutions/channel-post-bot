"""Telegram formatting, HTML and Markdown in the flows that take a post's text: /new, the edits in My posts, /broadcast."""
import asyncio
from types import SimpleNamespace

from telethon import types

from app.tgutil import pack_file_id

from .fakes import make_msg, photo_media
from .harness import norm
from .test_broadcast import broadcast, make_app as broadcast_app, posted
from .test_flows import make_app


def msg(text, entities=None):
    return SimpleNamespace(raw_text=text, entities=entities or [], media=None, grouped_id=None, forward=None)


def shapes(entities):
    return [(type(e).__name__, e.offset, e.length) for e in entities or []]


async def new_post(app, text, message=None, *, publish=True):
    """/new, the text, no buttons - and publish. Returns the post id."""
    await app.text("/new")
    await app.text(text, message=message if message is not None else msg(text))
    prompt = norm(app.out.last_text)
    await app.text("/skip")
    pid = int(app.out.callback_data("Publish").split(":")[1])
    if publish:
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
    return pid, prompt


# ------------------------------------------------------------------------------------------------- /new
def test_a_post_typed_in_markdown_is_published_with_the_formatting(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, prompt = await new_post(app, "**Attack on Titan** - [watch](https://t.me/x)\n> quoted")
        assert "Formatting read from Markdown" in prompt and "/formatting" in prompt and "buttons" in prompt.lower()
        post = await app.db.get_post(pid)
        assert post.text == "Attack on Titan - watch\nquoted"
        assert post.entities == [
            {"k": "bold", "o": 0, "l": 15},
            {"k": "url", "o": 18, "l": 5, "u": "https://t.me/x"},
            {"k": "quote", "o": 24, "l": 6, "c": False},
        ]
        kind, peer, text, kw = app.tg.sent[-1]
        assert text == "Attack on Titan - watch\nquoted"
        assert shapes(kw["formatting_entities"]) == [("MessageEntityBold", 0, 15), ("MessageEntityTextUrl", 18, 5), ("MessageEntityBlockquote", 24, 6)]
        assert kw["formatting_entities"][1].url == "https://t.me/x"
        await app.db.close()

    asyncio.run(run())


def test_a_post_typed_in_html_is_published_with_the_formatting(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, prompt = await new_post(app, "<b>Episode 12</b> &amp; <i>13</i><br><tg-spoiler>secret</tg-spoiler>")
        assert "Formatting read from HTML" in prompt
        kind, peer, text, kw = app.tg.sent[-1]
        assert text == "Episode 12 & 13\nsecret"
        assert shapes(kw["formatting_entities"]) == [("MessageEntityBold", 0, 10), ("MessageEntityItalic", 13, 2), ("MessageEntitySpoiler", 16, 6)]
        await app.db.close()

    asyncio.run(run())


def test_the_preview_shows_what_will_be_published(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "**Hi** there", publish=False)
        await app.press(f"pv:{pid}")
        kind, peer, text, kw = [e for e in app.tg.sent if not isinstance(e[1], types.InputPeerChannel)][-1]
        assert text == "Hi there" and shapes(kw["formatting_entities"]) == [("MessageEntityBold", 0, 2)]
        await app.db.close()

    asyncio.run(run())


def test_formatting_made_in_telegram_is_published_as_it_is(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        ents = [types.MessageEntityBold(0, 5), types.MessageEntityTextUrl(6, 4, "https://x.com"), types.MessageEntityBlockquote(11, 3, True)]
        text = "**x** <b>y</b> z"  # looks like markup, but Telegram's own formatting wins and the text stays as typed
        pid, prompt = await new_post(app, text, msg(text, ents))
        assert "Formatting read from" not in prompt
        kind, peer, sent, kw = app.tg.sent[-1]
        assert sent == text
        assert shapes(kw["formatting_entities"]) == [("MessageEntityBold", 0, 5), ("MessageEntityTextUrl", 6, 4), ("MessageEntityBlockquote", 11, 3)]
        assert kw["formatting_entities"][2].collapsed is True
        await app.db.close()

    asyncio.run(run())


def test_plain_text_is_published_untouched(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        text = "5 * 3 = 15 - snake_case_name - 1 < 2 - Sub | Dub - https://example.com/a_b_c"
        pid, prompt = await new_post(app, text)
        assert "Formatting read from" not in prompt
        kind, peer, sent, kw = app.tg.sent[-1]
        assert sent == text and kw["formatting_entities"] == []
        await app.db.close()

    asyncio.run(run())


def test_markup_input_off_keeps_everything_literal(tmp_path):
    async def run():
        from .harness import App

        app = App(tmp_path, markup_input=False)
        await app.start()
        await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
        pid, prompt = await new_post(app, "**kept** <b>as</b> typed")
        assert "Formatting read from" not in prompt
        kind, peer, sent, kw = app.tg.sent[-1]
        assert sent == "**kept** <b>as</b> typed" and kw["formatting_entities"] == []
        await app.db.close()

    asyncio.run(run())


def test_a_photo_with_a_markdown_caption(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/new")
        photo = SimpleNamespace(raw_text="**New** poster", entities=[], media=photo_media(5), grouped_id=None, forward=None)
        await app.text("**New** poster", message=photo)
        assert "Formatting read from Markdown" in norm(app.out.last_text)
        await app.text("/skip")
        pid = int(app.out.callback_data("Publish").split(":")[1])
        post = await app.db.get_post(pid)
        assert post.media_kind == "photo" and post.text == "New poster" and post.entities == [{"k": "bold", "o": 0, "l": 3}]
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        kind, peer, media, kw = app.tg.sent[-1]
        assert kind == "file" and kw["caption"] == "New poster" and shapes(kw["formatting_entities"]) == [("MessageEntityBold", 0, 3)]
        await app.db.close()

    asyncio.run(run())


# -------------------------------------------------------------------------------- editing in My posts
def test_the_text_of_a_published_post_can_be_changed_with_markdown(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "first")
        await app.press(f"et:{pid}")
        assert "HTML / Markdown" in norm(app.out.last_text) and "/formatting" in norm(app.out.last_text)
        await app.text("__new__ *text* here", message=msg("__new__ *text* here"))
        req = app.tg.edits[-1][2]
        assert req.message == "new text here"
        assert shapes(req.entities) == [("MessageEntityUnderline", 0, 3), ("MessageEntityItalic", 4, 4)]
        post = await app.db.get_post(pid)
        assert post.text == "new text here" and [e["k"] for e in post.entities] == ["underline", "italic"]
        assert "Formatting read from Markdown" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_the_text_of_a_draft_can_be_changed_with_html(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "first", publish=False)
        await app.press(f"et:{pid}")
        await app.text("<code>x = 1</code> and <a href=\"https://t.me/x\">link</a>", message=msg("<code>x = 1</code> and <a href=\"https://t.me/x\">link</a>"))
        post = await app.db.get_post(pid)
        assert post.status == "draft" and post.text == "x = 1 and link"
        assert post.entities == [{"k": "code", "o": 0, "l": 5}, {"k": "url", "o": 10, "l": 4, "u": "https://t.me/x"}]
        assert "Formatting read from HTML" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_a_telegram_formatted_edit_is_kept_and_says_nothing_about_markup(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "first")
        await app.press(f"et:{pid}")
        await app.text("*not italic*", message=msg("*not italic*", [types.MessageEntityBold(0, 12)]))
        post = await app.db.get_post(pid)
        assert post.text == "*not italic*" and post.entities == [{"k": "bold", "o": 0, "l": 12}]
        assert "Formatting read from" not in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_a_caption_edit_reads_markdown_too(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        post = await app.db.create_post(
            channel_id=1, message_id=7, status="sent", source="bot", text="old", entities=[], buttons=[],
            media_kind="photo", media_file_id=pack_file_id(make_msg(1, "", media=photo_media(3))),
        )
        await app.press(f"et:{post.id}")
        assert "caption" in norm(app.out.last_text)
        await app.text("||spoiler|| caption", message=msg("||spoiler|| caption"))
        saved = await app.db.get_post(post.id)
        assert saved.text == "spoiler caption" and saved.entities == [{"k": "spoiler", "o": 0, "l": 7}]
        req = app.tg.edits[-1][2]
        assert req.message == "spoiler caption" and shapes(req.entities) == [("MessageEntitySpoiler", 0, 7)]
        await app.db.close()

    asyncio.run(run())


def test_a_text_that_is_too_long_after_reading_is_refused(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "first")
        await app.press(f"et:{pid}")
        before = len(app.tg.edits)
        await app.text("x" * 4097)
        assert "too long" in norm(app.out.last_text).lower() or "at most" in norm(app.out.last_text).lower()
        assert len(app.tg.edits) == before and (await app.db.get_post(pid)).text == "first"
        await app.db.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------------------- /broadcast
def test_a_broadcast_typed_in_html_reaches_every_channel_formatted(tmp_path):
    async def run():
        app = await broadcast_app(tmp_path)
        await app.text("/broadcast 1h")
        text = "<b>Maintenance</b> tonight - <a href=\"https://t.me/x\">details</a>"
        await app.text(text, message=msg(text))
        assert "Formatting read from HTML" in norm(app.out.last_text)
        await app.press(app.out.callback_data("No buttons"))
        await app.press(app.out.callback_data("Send to all channels"))
        sent = posted(app)
        assert len(sent) == 2
        for kind, peer, body, kw in sent:
            assert body == "Maintenance tonight - details"
            assert shapes(kw["formatting_entities"]) == [("MessageEntityBold", 0, 11), ("MessageEntityTextUrl", 22, 7)]
        for cid in (1, 2):
            (p,) = await app.db.sent_posts(cid)
            assert p.text == "Maintenance tonight - details" and [e["k"] for e in p.entities] == ["bold", "url"]
        await app.db.close()

    asyncio.run(run())


def test_a_broadcast_keeps_telegram_formatting_and_plain_text(tmp_path):
    async def run():
        app = await broadcast_app(tmp_path)
        await broadcast(app, "/broadcast 1h", "**not read**", message=msg("**not read**", [types.MessageEntityItalic(0, 12)]))
        (first, _) = posted(app)
        assert first[2] == "**not read**" and shapes(first[3]["formatting_entities"]) == [("MessageEntityItalic", 0, 12)]
        await app.db.close()

    asyncio.run(run())


def test_a_broadcast_with_markup_input_off_stays_literal(tmp_path):
    async def run():
        app = await broadcast_app(tmp_path, markup_input=False)
        await broadcast(app, "/broadcast 1h", "**kept**", message=msg("**kept**"))
        (first, _) = posted(app)
        assert first[2] == "**kept**" and first[3]["formatting_entities"] == []
        assert "Formatting read from" not in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())
