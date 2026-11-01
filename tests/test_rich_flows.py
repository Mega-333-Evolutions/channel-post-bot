"""Rich messages through the bot: /new, the edits in My posts, the preview, /broadcast, the userbot, /swap, the sync and /repost."""
import asyncio
from types import SimpleNamespace

from telethon import functions, types

from app.replace_engine import ScanOptions, apply_channel, scan_channel, undo_batch
from app.repost_engine import RepostOptions, final_state, run_repost, stand_in
from app.richmsg import Rich, entry, fingerprint, rich_of
from app.suggest import build_suggestion
from app.sync_engine import diff_post, live_fields, message_supported, sync_channel
from app.tgutil import button_url, pack_file_id

from .fakes import FakeChannelClient, make_msg, markup, photo_media, rpc, url_btn, video_media
from .harness import App, norm
from .test_broadcast import broadcast, make_app as broadcast_app
from .test_engine import setup as engine_setup
from .test_flows import make_app
from .test_markup_flows import msg, new_post
from .test_replace_userbot import BUTTONS, CID, MID, make_app as userbot_app, replacement, user_edits
from .test_repost import NEW, OLD, edit_requests, forwarded_ids, setup as repost_setup
from .test_rich import blocks, rich_msg, same
from .test_sync import old_msg, opts, world

HEADING = "# Release notes\n\n- [x] fixed\n- [ ] todo\n\n| a | b |\n|---|---|\n| 1 | 2 |"
TABLE = '<h2>Schedule</h2><table bordered><tr><th>Day</th><th>Show</th></tr><tr><td>Mon</td><td>Naruto</td></tr></table>'


def native(rm=None, mid=77):
    """A rich message sent to the bot (or forwarded to it)."""
    return SimpleNamespace(
        raw_text="", entities=[], media=None, grouped_id=None, forward=None, rich_message=rm or rich_msg(), id=mid
    )


def raw_edits(app):
    """The messages.editMessage requests the bot sent, each once."""
    out = []
    for e in app.tg.edits:
        r = e[2] if isinstance(e, tuple) and e[0] == "raw" else e
        if isinstance(r, functions.messages.EditMessageRequest) and not any(r is o for o in out):
            out.append(r)
    return out


def rich_sends(app):
    return [e for e in app.tg.sent if e[0] == "rich"]


def md_post(source):
    return dict(text="x", entities=[entry(Rich("markdown", source))])


# ============================================================================================== typed text: /new
def test_markdown_with_a_heading_is_published_as_a_rich_message(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, prompt = await new_post(app, HEADING)
        assert "Rich message read from Markdown" in prompt and "/rich" in prompt and "buttons" in prompt.lower()
        post = await app.db.get_post(pid)
        assert post.status == "sent" and post.entities == [{"k": "rich", "f": "markdown", "s": HEADING}]
        assert post.text.startswith("Release notes\n") and "☑ fixed" in post.text and "☐ todo" in post.text
        kind, peer, inp, kw = app.tg.sent[-1]
        assert kind == "rich" and isinstance(peer, types.InputPeerChannel) and peer.channel_id == 1
        assert isinstance(inp, types.InputRichMessageMarkdown) and inp.markdown == HEADING and not inp.rtl
        assert kw["reply_markup"] is None
        live = app.tg.msgs[post.message_id]  # Telegram made a rich message of it
        assert live.rich_message is not None and live.message == ""
        assert [e for e in app.tg.sent if e[0] in ("text", "file")] == []  # nothing went out as an ordinary message
        await app.db.close()

    asyncio.run(run())


def test_html_with_a_table_is_published_as_a_rich_message(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, prompt = await new_post(app, TABLE)
        assert "Rich message read from HTML" in prompt
        post = await app.db.get_post(pid)
        assert post.entities == [{"k": "rich", "f": "html", "s": TABLE}]
        assert post.text == "Schedule\nDay | Show\nMon | Naruto"
        kind, peer, inp, kw = app.tg.sent[-1]
        assert kind == "rich" and isinstance(inp, types.InputRichMessageHTML) and inp.html == TABLE
        await app.db.close()

    asyncio.run(run())


def test_the_rich_marker_turns_any_text_into_a_rich_message_with_its_options(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, prompt = await new_post(app, "<rich rtl noautolink>\nhello **x** #tag @name")
        assert "Rich message read from Markdown" in prompt
        post = await app.db.get_post(pid)
        assert post.text == "hello x #tag @name"
        assert post.entities == [{"k": "rich", "f": "markdown", "s": "hello **x** #tag @name", "r": 1, "n": 1}]
        kind, peer, inp, kw = app.tg.sent[-1]
        assert isinstance(inp, types.InputRichMessageMarkdown) and inp.markdown == "hello **x** #tag @name"
        assert inp.rtl is True and inp.noautolink is True
        await app.db.close()

    asyncio.run(run())


def test_text_that_only_looks_a_little_like_markup_stays_an_ordinary_post(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        for text in ("#anime #naruto new episode", "Price: $5 and $6 - a == b - I <3 it\n- one\n- two\n---", "**bold** [x](https://t.me/x)"):
            pid, prompt = await new_post(app, text)
            assert "Rich message" not in prompt
            assert rich_of(await app.db.get_post(pid)) is None
            assert app.tg.sent[-1][0] == "text"
        await app.db.close()

    asyncio.run(run())


def test_markup_input_off_keeps_typed_blocks_literal(tmp_path):
    async def run():
        app = App(tmp_path, markup_input=False)
        await app.start()
        await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
        pid, prompt = await new_post(app, HEADING)
        assert "Rich message" not in prompt
        kind, peer, text, kw = app.tg.sent[-1]
        assert kind == "text" and text == HEADING and kw["formatting_entities"] == []
        # a rich message made in Telegram is not typed text: it is still kept
        pid, prompt = await new_post(app, "", message=native())
        assert "Rich message kept as it is" in prompt and app.tg.sent[-1][0] == "rich"
        await app.db.close()

    asyncio.run(run())


def test_a_typed_rich_message_that_is_too_long_is_refused(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/new")
        await app.text("# Title\n" + "x" * 33000, message=msg("# Title\n" + "x" * 33000))
        assert "32768" in norm(app.out.last_text)
        assert (await app.db.list_posts(1, None, 0, 10))[1] == 0  # nothing was saved
        await app.db.close()

    asyncio.run(run())


# =================================================================================== a rich message sent to the bot
def test_a_rich_message_sent_to_the_bot_is_published_block_for_block(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        rm = rich_msg(photos=[photo_media(5).photo], documents=[video_media(81).document], rtl=True)
        pid, prompt = await new_post(app, "", message=native(rm))
        assert "Rich message kept as it is" in prompt
        post = await app.db.get_post(pid)
        (e,) = post.entities
        assert (e["k"], e["f"], e["r"], e["p"]) == ("rich", "blocks", 1, fingerprint(rm)) and post.text == "Title\nHello"
        kind, peer, inp, kw = app.tg.sent[-1]
        assert kind == "rich" and isinstance(inp, types.InputRichMessage) and inp.rtl
        assert same(inp.blocks, rm.blocks)
        assert [(p.id, p.access_hash, p.file_reference) for p in inp.photos] == [(5, 35, b"ref5")]
        assert [d.id for d in inp.documents] == [81]
        live = app.tg.msgs[post.message_id].rich_message
        assert same(live.blocks, rm.blocks) and [p.id for p in live.photos] == [5]
        await app.db.close()

    asyncio.run(run())


def test_a_rich_message_that_arrives_in_parts_is_asked_for_whole(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        part = rich_msg([types.PageBlockParagraph(types.TextPlain("first"))], part=True)
        app.tg.full_rich = {77: rich_msg(blocks("whole"))}
        pid, prompt = await new_post(app, "", message=native(part))
        assert "Rich message kept as it is" in prompt
        assert (await app.db.get_post(pid)).text == "Title\nwhole"
        assert any(isinstance(r, functions.messages.GetRichMessageRequest) and r.id == 77 for r in app.tg.requests)
        await app.db.close()

    asyncio.run(run())


def test_a_rich_message_that_cannot_be_had_whole_makes_no_post(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        part = rich_msg([types.PageBlockParagraph(types.TextPlain("first"))], part=True)
        app.tg.full_rich = {77: rich_msg(blocks(), part=True)}
        await app.text("/new")
        await app.text("", message=native(part))
        assert "part of that rich message" in norm(app.out.last_text)
        assert (await app.db.list_posts(1, None, 0, 10))[1] == 0
        assert app.ctx.state[app.uid]["mode"] == "new_content"  # still waiting for the message
        await app.text("hello", message=msg("hello"))  # and a better one is taken
        assert (await app.db.list_posts(1, None, 0, 10))[1] == 1
        await app.db.close()

    asyncio.run(run())


# ================================================================================ buttons, preview, publishing
def test_the_link_buttons_of_a_post_go_with_its_rich_message(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/new")
        await app.text(HEADING, message=msg(HEADING))
        await app.text("Download - https://t.me/x?start=a | Other - https://example.com", message=msg("x"))
        pid = int(app.out.callback_data("Publish").split(":")[1])
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        kind, peer, inp, kw = app.tg.sent[-1]
        rows = kw["reply_markup"].rows
        assert kind == "rich" and [b.text for b in rows[0].buttons] == ["Download", "Other"]
        assert [button_url(b) for b in rows[0].buttons] == ["https://t.me/x?start=a", "https://example.com"]
        assert app.tg.msgs[(await app.db.get_post(pid)).message_id].reply_markup is not None
        await app.db.close()

    asyncio.run(run())


def test_the_preview_of_a_rich_draft_goes_to_the_owner_and_not_to_the_channel(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, HEADING, publish=False)
        await app.press(f"pv:{pid}")
        kind, peer, inp, kw = app.tg.sent[-1]
        assert kind == "rich" and isinstance(peer, types.InputPeerUser) and inp.markdown == HEADING
        assert app.tg.msgs == {} and (await app.db.get_post(pid)).status == "draft"
        await app.db.close()

    asyncio.run(run())


def test_telegram_not_being_able_to_make_the_message_is_explained_in_the_preview_and_when_publishing(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "<rich>\n<tg-map lat=\"x\"/>", publish=False)
        app.tg.fail["SendMessageRequest"] = rpc("MessageEmptyError")
        await app.press(f"pv:{pid}")
        assert "see /rich for the syntax" in norm(app.out.last_text)
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        assert "NOT published" in norm(app.out.last_text)
        assert (await app.db.get_post(pid)).status == "draft" and app.tg.msgs == {}
        await app.db.close()

    asyncio.run(run())


def test_the_panel_of_a_rich_post_has_no_media_or_link_preview(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, HEADING, publish=False)
        await app.press(f"po:{pid}")
        text = norm(app.out.last_text)
        assert "Type: 🧩 rich message (typed as Markdown)" in text and "Release notes" in text
        labels = [b for row in app.out.last_buttons() for b in row]
        assert "✏️ Text" in labels and "👁 Preview" in labels and "➕ Button" in labels
        assert not any("Media" in b or "Link preview" in b for b in labels if b)
        native_pid, _ = await new_post(app, "", message=native(), publish=False)
        assert "made in Telegram" in norm((await _panel(app, native_pid)))
        await app.db.close()

    asyncio.run(run())


async def _panel(app, pid):
    await app.press(f"po:{pid}")
    return app.out.last_text


# ====================================================================================== editing in My posts
def test_the_text_of_a_published_rich_post_is_changed_with_a_new_rich_message(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "# One\n\ntext")
        before = app.tg.msgs[(await app.db.get_post(pid)).message_id].rich_message
        await app.press(f"et:{pid}")
        assert "/rich" in norm(app.out.last_text)
        new = "# Two\n\n- [ ] a"
        await app.text(new, message=msg(new))
        (req,) = raw_edits(app)
        assert isinstance(req.rich_message, types.InputRichMessageMarkdown) and req.rich_message.markdown == new
        assert req.message is None and not req.entities
        post = await app.db.get_post(pid)
        assert post.entities == [{"k": "rich", "f": "markdown", "s": new}] and post.text == "Two\n\n☐ a"
        assert bytes(app.tg.msgs[post.message_id].rich_message) != bytes(before)  # the channel shows the new one
        assert "Rich message read from Markdown" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_a_native_rich_message_can_replace_the_text_of_a_post(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "plain words")
        await app.press(f"et:{pid}")
        await app.text("", message=native(rich_msg(blocks("brand new"))))
        (req,) = raw_edits(app)
        assert isinstance(req.rich_message, types.InputRichMessage) and req.message is None
        post = await app.db.get_post(pid)
        assert rich_of(post).fmt == "blocks" and post.text == "Title\nbrand new"
        assert "Rich message kept as it is" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_a_plain_post_becomes_a_rich_one_and_a_rich_one_a_plain_one_again(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "hello")
        await app.press(f"et:{pid}")
        await app.text(HEADING, message=msg(HEADING))
        assert isinstance(raw_edits(app)[-1].rich_message, types.InputRichMessageMarkdown)
        assert rich_of(await app.db.get_post(pid)) is not None
        await app.press(f"et:{pid}")
        await app.text("plain again", message=msg("plain again"))
        last = raw_edits(app)[-1]
        assert last.message == "plain again" and last.rich_message is None
        post = await app.db.get_post(pid)
        assert rich_of(post) is None and post.text == "plain again" and post.entities == []
        await app.db.close()

    asyncio.run(run())


def test_a_draft_can_be_changed_into_a_rich_message_before_it_is_published(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "first", publish=False)
        await app.press(f"et:{pid}")
        await app.text(TABLE, message=msg(TABLE))
        post = await app.db.get_post(pid)
        assert post.status == "draft" and rich_of(post).fmt == "html" and raw_edits(app) == []
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        assert app.tg.sent[-1][0] == "rich" and app.tg.sent[-1][2].html == TABLE
        await app.db.close()

    asyncio.run(run())


def test_the_buttons_of_a_published_rich_post_are_changed_without_losing_the_message(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, HEADING)
        await app.press(f"ba:{pid}")
        await app.text("Watch", message=msg("Watch"))
        await app.text("https://example.com/w", message=msg("https://example.com/w"))
        req = raw_edits(app)[-1]
        assert isinstance(req.rich_message, types.InputRichMessageMarkdown) and req.rich_message.markdown == HEADING
        assert [button_url(b) for b in req.reply_markup.rows[0].buttons] == ["https://example.com/w"]
        post = await app.db.get_post(pid)
        assert rich_of(post) is not None and post.buttons == [[{"t": "Watch", "u": "https://example.com/w"}]]
        await app.db.close()

    asyncio.run(run())


def test_a_rich_post_has_no_media_slot(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, HEADING)
        await app.press(f"em:{pid}")
        assert "has no media slot" in norm(app.out.last_text)
        assert app.ctx.state.get(app.uid) is None or app.ctx.state[app.uid]["mode"] != "edit_media"
        await app.db.close()

    asyncio.run(run())


def test_a_caption_is_never_a_rich_message(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        post = await app.db.create_post(
            channel_id=1, message_id=7, status="sent", source="bot", text="old", entities=[], buttons=[],
            media_kind="photo", media_file_id=pack_file_id(make_msg(1, "", media=photo_media(3))),
        )
        await app.press(f"et:{post.id}")
        assert "Rich messages" not in norm(app.out.last_text)
        await app.text("", message=native())  # a rich message as a caption
        assert "caption can't be a rich message" in norm(app.out.last_text)
        await app.text("# Heading\n\n- [ ] task", message=msg("# Heading\n\n- [ ] task"))  # typed blocks stay words
        saved_post = await app.db.get_post(post.id)
        assert rich_of(saved_post) is None and saved_post.text.startswith("# Heading") and saved_post.media_kind == "photo"
        assert all(r.rich_message is None for r in raw_edits(app))
        await app.db.close()

    asyncio.run(run())


def test_a_rich_message_can_not_be_made_a_media_post_by_the_media_edit(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/new")
        await app.text("", message=SimpleNamespace(raw_text="", entities=[], media=photo_media(5), grouped_id=None, forward=None, rich_message=rich_msg()))
        post = (await app.db.list_posts(1, None, 0, 10))[0][0]
        assert post.media_kind == "photo" and rich_of(post) is None  # a photo wins: the message is not a rich post
        await app.db.close()

    asyncio.run(run())


# =================================================================================================== /broadcast
def test_a_broadcast_can_be_a_rich_message(tmp_path):
    async def run():
        app = await broadcast_app(tmp_path)
        await app.text("/broadcast 1h")
        assert "/rich" in norm(app.out.last_text)
        text = "# Maintenance\n\n- tonight 22:00\n- [ ] backups"
        await app.text(text, message=msg(text))
        assert "Rich message read from Markdown" in norm(app.out.last_text)
        await app.press(app.out.callback_data("No buttons"))
        assert rich_sends(app)[-1][1].__class__ is types.InputPeerUser  # the preview was made as a rich message too
        await app.press(app.out.callback_data("Send to all channels"))
        for cid in (1, 2):
            chan = app.tg.registry[cid]
            (live,) = [m for m in chan.msgs.values() if m.rich_message is not None]
            assert isinstance(live.rich_input, types.InputRichMessageMarkdown) and live.rich_input.markdown == text
            (p,) = await app.db.sent_posts(cid)
            assert p.message_id == live.id and p.entities == [{"k": "rich", "f": "markdown", "s": text}]
            assert p.text.startswith("Maintenance")
        assert len(await app.db.pending_deletes()) == 2  # it is still deleted when its time is up
        await app.db.close()

    asyncio.run(run())


def test_a_broadcast_with_a_native_rich_message_and_buttons(tmp_path):
    async def run():
        app = await broadcast_app(tmp_path)
        await broadcast(app, "/broadcast 1h", "", buttons="Open - https://t.me/x", message=native())
        for cid in (1, 2):
            (live,) = [m for m in app.tg.registry[cid].msgs.values() if m.rich_message is not None]
            assert same(live.rich_message.blocks, blocks())
            assert [button_url(b) for b in live.reply_markup.rows[0].buttons] == ["https://t.me/x"]
        await app.db.close()

    asyncio.run(run())


# ======================================================================================================== /replace
def test_replace_leaves_a_rich_post_alone(tmp_path):
    async def run():
        app, chan, user = await userbot_app(tmp_path)
        live = chan.msgs[MID]
        live.media, live.message, live.rich_message = None, "", rich_msg()  # the channel's post is a rich message
        await app.text(f"/replace https://t.me/c/{CID}/{MID}", reply=replacement())
        assert "is a rich message" in norm(app.out.last_text)
        assert user_edits(user) == [] and not any(isinstance(r, functions.messages.EditMessageRequest) for r in chan.requests)
        assert chan.msgs[MID].rich_message is not None
        await app.db.close()

    asyncio.run(run())


# ================================================================================================ the userbot
def test_a_rich_edit_that_telegram_refuses_the_bot_is_made_by_the_userbot(tmp_path):
    async def run():
        app, chan, user = await userbot_app(tmp_path)  # the post belongs to another bot: the bot may not edit it
        live = chan.msgs[MID]
        live.media, live.message, live.rich_message = None, "", chan._rich(types.InputRichMessageMarkdown(markdown="# old"))
        post = await app.db.create_post(
            channel_id=CID, message_id=MID, status="sent", source="adopted", text="old",
            entities=[entry(Rich("markdown", "# old"))], buttons=BUTTONS,
        )
        await app.press(f"et:{post.id}")
        new = "# New\n\n- [x] a"
        await app.text(new, message=msg(new))
        (req,) = user_edits(user)
        assert isinstance(req.rich_message, types.InputRichMessageMarkdown) and req.rich_message.markdown == new
        assert req.message is None and req.reply_markup is None  # a user account sends no keyboard
        assert isinstance(chan.msgs[MID].rich_message, types.RichMessage) and chan.msgs[MID].rich_input.markdown == new
        assert "the userbot made the edit" in norm(app.out.last_text) and "Rich message read from Markdown" in norm(app.out.last_text)
        saved_post = await app.db.get_post(post.id)
        assert saved_post.entities == [{"k": "rich", "f": "markdown", "s": new}] and saved_post.buttons == BUTTONS
        await app.db.close()

    asyncio.run(run())


def test_the_userbot_editor_can_set_a_rich_message():
    from .test_userbot import channel
    from .test_userbot_edit import ready_editor

    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="", entities=[], rich=Rich("html", "<h1>New</h1>"))
        await e.close()
        req = bot.user_edits[-1]
        assert isinstance(req.rich_message, types.InputRichMessageHTML) and req.rich_message.html == "<h1>New</h1>"
        assert req.message is None and bot.msgs[5].rich_message is not None and bot.msgs[5].message == ""

    asyncio.run(go())


# ============================================================================================ /swap and /handleswap
OLD_LINK = "https://t.me/bharath?start=AAA"


def live_rich_post(i=1, *, out=True):
    m = make_msg(i, "", markup=markup([url_btn("Get", OLD_LINK)]), out=out)
    m.rich_message = rich_msg(
        [types.PageBlockParagraph(types.TextUrl(types.TextPlain("t.me/bharath"), "https://t.me/bharath?start=ZZZ", 0))]
    )
    return m


def test_swap_changes_only_the_buttons_of_a_rich_post_and_undo_puts_them_back(tmp_path):
    async def run():
        live = live_rich_post()
        db, ch, client = await engine_setup(tmp_path, {1: live})
        typed = Rich("markdown", "# Title\n\n@bharath t.me/bharath")
        await db.create_post(
            channel_id=1, message_id=1, status="sent", source="bot", text="Title", entities=[entry(typed)],
            buttons=[[{"t": "Get", "u": OLD_LINK}]],
        )
        o = ScanOptions("bharath", "goku")
        scan = await scan_channel(client, ch, o, can_edit_others=True)
        assert set(scan.infos) == {1}
        await db.create_batch("b1", "bharath", "goku", 99)
        res = await apply_channel(client, db, scan, o, "b1", 99, edit_delay=0)
        assert (res.edited, res.failed) == (1, [])
        req = client.edits[-1]
        assert req.message is None and req.rich_message is None  # only the keyboard was sent
        assert button_url(live.reply_markup.rows[0].buttons[0]) == "https://t.me/goku?start=AAA"
        assert live.message == "" and live.rich_message is not None
        p = await db.find_by_message(1, 1)
        assert rich_of(p) == typed and p.text == "Title" and p.buttons == [[{"t": "Get", "u": "https://t.me/goku?start=AAA"}]]

        ures = await undo_batch(client, db, "b1", edit_delay=0)
        assert ures.restored == 1 and not ures.failed
        assert client.edits[-1].message is None and client.edits[-1].rich_message is None
        assert button_url(live.reply_markup.rows[0].buttons[0]) == OLD_LINK
        p = await db.find_by_message(1, 1)
        assert rich_of(p) == typed and p.text == "Title" and p.buttons == [[{"t": "Get", "u": OLD_LINK}]]
        await db.close()

    asyncio.run(run())


def test_a_rich_post_nobody_saved_is_adopted_by_a_swap_as_its_blocks(tmp_path):
    async def run():
        live = live_rich_post()
        db, ch, client = await engine_setup(tmp_path, {1: live})
        o = ScanOptions("bharath", "goku")
        scan = await scan_channel(client, ch, o, can_edit_others=True)
        await db.create_batch("b1", "bharath", "goku", 99)
        await apply_channel(client, db, scan, o, "b1", 99, edit_delay=0)
        p = await db.find_by_message(1, 1)
        r = rich_of(p)
        assert p.source == "adopted" and r.fmt == "blocks" and r.fp == fingerprint(live.rich_message)
        assert p.text == "t.me/bharath" and p.buttons == [[{"t": "Get", "u": "https://t.me/goku?start=AAA"}]]
        await db.close()

    asyncio.run(run())


def test_swaps_leave_the_text_of_a_rich_post_alone(tmp_path):
    async def run():
        for mode in ("links", "handles"):
            live = make_msg(1, "")
            live.rich_message = rich_msg([types.PageBlockParagraph(types.TextPlain("@bharath https://t.me/bharath?start=A"))])
            db, ch, client = await engine_setup(tmp_path, {1: live})
            scan = await scan_channel(client, ch, ScanOptions("bharath", "goku", mode=mode), can_edit_others=True)
            assert scan.infos == {}  # no button to change: nothing to do
            await db.close()

    asyncio.run(run())


def test_a_handleswap_renames_the_buttons_of_a_rich_post_only(tmp_path):
    async def run():
        live = make_msg(1, "", markup=markup([url_btn("Join @bharath", "https://example.com")]), out=True)
        live.rich_message = rich_msg()
        db, ch, client = await engine_setup(tmp_path, {1: live})
        o = ScanOptions("bharath", "goku", mode="handles")
        scan = await scan_channel(client, ch, o, can_edit_others=True)
        await db.create_batch("b1", "bharath", "goku", 99)
        await apply_channel(client, db, scan, o, "b1", 99, edit_delay=0)
        assert live.reply_markup.rows[0].buttons[0].text == "Join @goku" and client.edits[-1].message is None
        assert live.rich_message is not None
        await db.close()

    asyncio.run(run())


# ===================================================================================================== the sync
class SavedLike:
    """Anything with the Post fields."""

    def __init__(self, m, **kw):
        for k, v in live_fields(m).items():
            setattr(self, k, v)
        self.link_preview = False
        for k, v in kw.items():
            setattr(self, k, v)


def rich_live(i, word="Hello", **kw):
    m = old_msg(i, "", **kw)
    m.rich_message = rich_msg(blocks(word))
    return m


def test_a_saved_rich_post_that_is_unchanged_shows_no_difference():
    m = rich_live(1)
    p = SavedLike(m)
    d = diff_post(p, m)
    assert d.fields == {} and not d.quiet and not d.buttons_missing
    assert message_supported(m) and not message_supported(old_msg(2, ""))


def test_a_rich_post_edited_in_the_channel_is_taken_over_as_its_new_blocks():
    m = rich_live(1, "Hello")
    p = SavedLike(m)
    edited = rich_live(1, "Changed")
    d = diff_post(p, edited)
    assert set(d.fields) == {"text", "entities"} and d.fields["text"] == "Title\nChanged"
    (e,) = d.fields["entities"]
    assert e["f"] == "blocks" and e["p"] == fingerprint(edited.rich_message) and not d.quiet


def test_the_bots_own_typed_rich_post_is_only_noted_the_first_time_the_sync_sees_it():
    m = rich_live(1)
    typed = Rich("markdown", HEADING)
    p = SavedLike(m, text="Release notes", entities=[entry(typed)])
    d = diff_post(p, m)
    assert d.quiet and set(d.fields) == {"entities"}
    (e,) = d.fields["entities"]
    assert (e["f"], e["s"], e["p"]) == ("markdown", HEADING, fingerprint(m.rich_message))  # its source is kept, the fingerprint added
    again = SavedLike(m, text="Release notes", entities=d.fields["entities"])
    assert diff_post(again, m).fields == {}
    changed = rich_live(1, "Edited by an admin")
    assert set(diff_post(again, changed).fields) == {"text", "entities"}


def test_a_rich_post_that_became_an_ordinary_one_or_the_other_way_round_is_followed():
    rich = rich_live(1)
    plain = old_msg(1, "now just words")
    d = diff_post(SavedLike(rich), plain)
    assert d.fields == {"text": "now just words", "entities": []}
    d = diff_post(SavedLike(plain), rich)
    assert d.fields["text"] == "Title\nHello" and d.fields["entities"][0]["f"] == "blocks"
    empty = old_msg(1, "")  # Telegram shows nothing this time: that is not an edit
    assert diff_post(SavedLike(rich), empty).fields == {}


def test_the_sync_saves_a_rich_post_it_finds_and_leaves_it_alone_afterwards(tmp_path):
    async def run():
        chan, db, ch = await world(tmp_path, {1: old_msg(1, "plain"), 2: rich_live(2)}, save=False)
        rep = await sync_channel(chan, db, ch, opts())
        p = await db.find_by_message(1, 2)
        assert p is not None and rich_of(p).fmt == "blocks" and p.text == "Title\nHello" and p.source == "adopted"
        again = await sync_channel(chan, db, ch, opts())
        assert again.edited == [] and again.adopted == [] and rep.error is None
        chan.msgs[2].rich_message = rich_msg(blocks("Later"))  # somebody edits it in the channel
        third = await sync_channel(chan, db, ch, opts())
        assert third.edited == [2] and (await db.find_by_message(1, 2)).text == "Title\nLater"
        await db.close()

    asyncio.run(run())


# ===================================================================================================== /repost
def rich_channel():
    welcome = make_msg(1, "Welcome")
    same_post = make_msg(2, "")
    same_post.rich_message = rich_msg(blocks("one"))
    with_button = make_msg(3, "", markup=markup([url_btn("Get", f"https://t.me/{OLD}?start=AAA"), url_btn("Other", "https://example.com/x")]))
    with_button.rich_message = rich_msg(blocks("two"), photos=[photo_media(5).photo])
    return {1: welcome, 2: same_post, 3: with_button}


def test_a_repost_copies_rich_posts_and_rebuilds_the_one_with_buttons(tmp_path):
    async def run():
        db, ch, chan, mig = await repost_setup(tmp_path, FakeChannelClient(rich_channel()), include_typed=True)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.aborted) == (3, [], None)
        assert forwarded_ids(chan) == [[1], [2]] and edit_requests(chan) == []
        new = {i: m for i, m in chan.msgs.items() if i > 3}
        assert list(new) == [4, 5, 6]
        assert new[4].message == "Welcome"
        assert same(new[5].rich_message.blocks, blocks("one"))  # a copy by Telegram: still the same rich message
        m = new[6]  # the post with buttons is created again, with the link swapped
        assert isinstance(m.rich_input, types.InputRichMessage) and same(m.rich_input.blocks, blocks("two"))
        assert [(p.id, p.file_reference) for p in m.rich_input.photos] == [(5, b"ref5")]
        rows = m.reply_markup.rows
        assert [button_url(b) for b in rows[0].buttons] == [f"https://t.me/{NEW}?start=AAA", "https://example.com/x"]
        saved_row = await db.find_by_message(1, 6)
        assert saved_row.text == "Title\ntwo" and rich_of(saved_row).fmt == "blocks"
        assert saved_row.buttons[0][0]["u"] == f"https://t.me/{NEW}?start=AAA"
        assert rich_of(await db.find_by_message(1, 5)) is not None
        await db.close()

    asyncio.run(run())


def test_a_channel_that_forbids_forwarding_gets_its_rich_posts_rebuilt(tmp_path):
    async def run():
        chan = FakeChannelClient(rich_channel())
        chan.restrict_forwards = True
        db, ch, chan, mig = await repost_setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed) == (3, []) and forwarded_ids(chan) == [[1]]  # only the first, refused, try
        new = {i: m for i, m in chan.msgs.items() if i > 3}
        assert same(new[5].rich_input.blocks, blocks("one")) and same(new[6].rich_input.blocks, blocks("two"))
        assert new[5].reply_markup is None and new[6].reply_markup is not None
        await db.close()

    asyncio.run(run())


def test_a_post_held_back_by_telegram_is_rebuilt_from_its_saved_rich_message():
    row = SimpleNamespace(
        text="Release notes", entities=[entry(Rich("markdown", HEADING))], buttons=[], media_file_id=None, link_preview=False
    )
    live = make_msg(7, "This message couldn't be displayed on your device due to copyright infringement.")
    final = final_state(stand_in(live, row), RepostOptions(OLD, NEW))
    assert final.rich == Rich("markdown", HEADING) and final.text == "" and final.entities == []
    assert final_state(make_msg(8, "plain"), RepostOptions(OLD, NEW)).rich is None


# ============================================================================================== everything else
def test_a_rich_post_is_kept_by_an_export_and_an_import(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, HEADING)
        data = await app.db.export_all()
        (p,) = [x for x in data["posts"] if x["id"] == pid]
        assert p["entities"] == [{"k": "rich", "f": "markdown", "s": HEADING}]
        await app.db.close()

    asyncio.run(run())


def test_the_next_post_suggestion_leaves_rich_posts_out():
    post = SimpleNamespace(
        text="x", entities=[entry(Rich("markdown", HEADING))], buttons=[[{"t": "Download Episodes 1 to 20", "u": "https://t.me/x?start=a"}]],
    )
    assert build_suggestion(post) is None


def test_the_rich_command_and_the_help_pages(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/rich")
        said = [norm(t) for kind, t, _ in app.out.log if kind == "say"]
        assert len(said) == 2 and "Rich messages" in said[0] and "<tg-map" in said[1] and "# Heading" in said[0]
        await app.text("/help")
        assert "/rich" in norm(app.out.last_text)
        await app.text("/formatting")
        assert "/rich" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())
