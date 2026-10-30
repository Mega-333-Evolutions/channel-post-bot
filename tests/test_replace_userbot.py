"""/replace and the edits in My posts when Telegram refuses the bot: the userbot makes the edit instead."""
import asyncio

import pytest
from telethon import types

from app.common import UserError, apply_edit_reported
from app.tgutil import media_info, pack_file_id
from app.userbot import Userbot

from .fakes import FakeChannelClient, FakeUserClient, make_msg, markup, photo_media, rpc, url_btn
from .harness import App, norm

CID, MID = 1723201445, 9084
FULL = dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=True)
LINK = "https://t.me/otherbot?start=abc"
BUTTONS = [[{"t": "Download", "u": LINK}]]


async def make_app(tmp_path, *, userbot=True, foreign=True, rights=FULL, **cfg):
    """A channel with a button post. `foreign`: the post belongs to another bot, so Telegram refuses our bot's edits.

    Returns (app, the simulated channel, the userbot account's connection)."""
    app = App(tmp_path, userbot_session="S" if userbot else "", **cfg)
    await app.start()
    old = make_msg(MID, "old caption", media=photo_media(10), markup=markup([url_btn("Download", LINK)]))
    old.via_other_bot = foreign
    chan = app.tg.add_channel(FakeChannelClient({MID: old}, cid=CID, title="Database Channel"))
    chan.rights = dict(rights)
    await app.db.save_channel(CID, 5, "Database Channel", None, 1)
    user = FakeUserClient(chan)
    if userbot:
        app.ctx.userbot = Userbot(app.tg, app.ctx.cfg, client_factory=lambda c: user)
    app.tg._media_data[("photo", 20)] = b"NEW PHOTO"  # what the owner's new photo looks like
    return app, chan, user


def replacement(caption="new caption", entities=None):
    return make_msg(1, caption, entities, media=photo_media(20))


def said(app):
    return [norm(t) for kind, t, _ in app.out.log if kind == "say"]


async def replace(app, **kw):
    await app.text(f"/replace https://t.me/c/{CID}/{MID}", reply=replacement(**kw))


async def the_post(app):
    return await app.db.find_by_message(CID, MID)


# ------------------------------------------------------------------------------- the userbot steps in
def test_when_telegram_refuses_the_bot_the_userbot_replaces_media_and_caption(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        await replace(app)
        m = chan.msgs[MID]
        assert m.message == "new caption" and m.media.photo.id != 10 and m.media.photo.id != 20
        assert chan._media_data[("photo", m.media.photo.id)] == b"NEW PHOTO"  # the bot downloaded it, the userbot uploaded it
        assert len(user.uploads) == 1 and user.uploads[0][2] == len(b"NEW PHOTO")
        # the post in My posts shows what the channel shows now, and keeps its saved buttons
        post = await the_post(app)
        assert post.text == "new caption" and post.buttons == BUTTONS
        assert post.media_file_id == media_info(m)[1] != pack_file_id(replacement())
        # the owner is told what happened, in order
        first, last = said(app)[0], said(app)[-1]
        assert "Telegram refused the bot, so the userbot is making the edit" in first
        assert "Replaced the media in Database Channel" in last and "Telegram refused the bot (MessageIdInvalidError)" in last
        # the helper left things as it found them
        assert chan.admins == {} and all(i["revoked"] for i in chan.invites.values()) and not app.ctx.userbot.lock.locked()
        await app.db.close()

    asyncio.run(run())


def test_telegram_drops_the_buttons_and_the_bot_may_not_put_them_back(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        await replace(app)
        assert chan.msgs[MID].reply_markup is None  # a user account sets no keyboard
        last = said(app)[-1]
        assert "Telegram dropped the buttons with that edit and the bot may not put them back" in last
        assert "My posts" in last and "Check buttons" in last
        assert (await the_post(app)).buttons == BUTTONS  # still saved: Check buttons can try again
        await app.db.close()

    asyncio.run(run())


def test_the_bot_puts_the_buttons_back_when_it_may(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        chan.user_edit_frees_message = True  # once the other bot's keyboard is gone, the bot may set one
        await replace(app)
        live = chan.msgs[MID].reply_markup
        assert live is not None and live.rows[0].buttons[0].type.url == LINK
        assert "Telegram dropped the buttons with that edit, so the bot put them back" in said(app)[-1]
        await app.db.close()

    asyncio.run(run())


def test_buttons_that_stay_are_reported_as_kept(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        chan.user_edit_keeps_markup = True
        await replace(app)
        assert chan.msgs[MID].reply_markup is not None
        assert "The buttons are still on the post." in said(app)[-1]
        await app.db.close()

    asyncio.run(run())


def test_the_userbot_is_not_asked_when_the_bot_can_do_it(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, foreign=False)
        await replace(app)
        assert user.requests == [] and not user.connected  # nobody else was bothered
        assert app.tg.edits[-1][1] == MID and "Replaced the media" in said(app)[-1]
        assert "userbot" not in said(app)[-1]
        await app.db.close()

    asyncio.run(run())


# -------------------------------------------------------------------------- when nobody can do it
def test_without_a_userbot_the_owner_is_pointed_to_it_and_nothing_changes(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, userbot=False)
        await replace(app)
        text = said(app)[-1]
        assert "Telegram refused (MessageIdInvalidError)" in text and "see /userbot" in text and "Nothing was changed" in text
        assert chan.msgs[MID].message == "old caption" and chan.msgs[MID].media.photo.id == 10
        assert (await the_post(app)).text == "old caption"
        await app.db.close()

    asyncio.run(run())


def test_a_userbot_that_cannot_get_the_right_is_explained_and_nothing_changes(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, rights=dict(FULL, add_admins=False))
        await replace(app)
        text = said(app)[-1]
        assert "Telegram refused (MessageIdInvalidError)" in text and "The userbot could not do it either" in text
        assert "may not edit other people's posts" in text and "Add new admins" in text and "Nothing was changed" in text
        assert chan.msgs[MID].message == "old caption" and (await the_post(app)).text == "old caption"
        assert not app.ctx.userbot.lock.locked()
        await app.db.close()

    asyncio.run(run())


def test_a_userbot_that_telegram_refuses_too_is_reported(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        chan.fail["user:EditMessageRequest"] = rpc("MessageIdInvalidError")
        await replace(app)
        text = said(app)[-1]
        assert "The userbot could not do it either" in text and "Telegram refused the userbot as well" in text
        assert chan.admins == {} and (await the_post(app)).text == "old caption"
        await app.db.close()

    asyncio.run(run())


def test_a_connected_but_broken_userbot_session_is_named(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        user.authorized = False  # the session was revoked
        await replace(app)
        text = said(app)[-1]
        assert "not logged in any more" in text and "Nothing was changed" in text
        await app.db.close()

    asyncio.run(run())


def test_only_text_media_and_caption_edits_go_to_the_userbot(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        await replace(app)  # adopts the post
        post = await the_post(app)
        ch = await app.db.get_channel(CID)
        user.requests.clear()
        chan.msgs[MID].via_other_bot = True
        with pytest.raises(UserError) as exc:  # buttons can only be set by a bot
            await apply_edit_reported(app.ctx, post, ch, buttons=[[{"t": "X", "u": "https://x.com"}]])
        assert "Nothing was changed" in str(exc.value) and user.requests == []
        chan.fail["EditMessageRequest"] = rpc("EntityBoundsInvalidError")  # the text itself is what Telegram dislikes
        with pytest.raises(UserError) as exc:
            await apply_edit_reported(app.ctx, post, ch, text="fine", entities=[{"k": "bold", "o": 0, "l": 99}])
        assert "EntityBoundsInvalidError" in str(exc.value) and user.requests == []
        await app.db.close()

    asyncio.run(run())


# ------------------------------------------------------------------------ the edits in My posts
def test_a_caption_typed_in_markdown_is_edited_by_the_userbot_when_the_bot_is_refused(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        post = await app.db.create_post(
            channel_id=CID, message_id=MID, status="sent", source="adopted", text="old caption", entities=[],
            buttons=BUTTONS, media_kind="photo", media_file_id=pack_file_id(replacement()),
        )
        await app.press(f"et:{post.id}")
        assert "HTML / Markdown" in app.out.last_text
        await app.text("**new** caption")
        m = chan.msgs[MID]
        assert m.message == "new caption" and [(type(e).__name__, e.offset, e.length) for e in m.entities] == [("MessageEntityBold", 0, 3)]
        assert m.media.photo.id == 10  # only the caption changed, the photo stays
        assert user.uploads == []
        note = norm(app.out.last_text)
        assert "Formatting read from Markdown" in note and "the userbot made the edit" in note
        saved = await app.db.get_post(post.id)
        assert saved.text == "new caption" and saved.entities == [{"k": "bold", "o": 0, "l": 3}]
        await app.db.close()

    asyncio.run(run())


def test_new_media_sent_to_the_post_panel_is_uploaded_by_the_userbot(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        post = await app.db.create_post(
            channel_id=CID, message_id=MID, status="sent", source="adopted", text="old caption", entities=[],
            buttons=BUTTONS, media_kind="photo", media_file_id=pack_file_id(make_msg(2, "", media=photo_media(10))),
        )
        await app.press(f"em:{post.id}")
        await app.text("", message=replacement(caption=""))
        m = chan.msgs[MID]
        assert m.message == "old caption" and m.media.photo.id not in (10, 20)
        assert chan._media_data[("photo", m.media.photo.id)] == b"NEW PHOTO"
        assert any("Telegram refused the bot, so the userbot is making the edit" in t for t in said(app))
        assert "the userbot made the edit" in norm(app.out.last_text)
        assert (await app.db.get_post(post.id)).media_file_id == media_info(m)[1]
        await app.db.close()

    asyncio.run(run())


# ------------------------------------------------------------- Telegram's own formatting, HTML, Markdown
def sent_caption(app):
    kw = app.tg.edits[-1][2]
    return kw["text"], [(type(e).__name__, e.offset, e.length) for e in kw["formatting_entities"]]


def test_replace_keeps_the_formatting_made_in_telegram(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, foreign=False)
        ents = [types.MessageEntityItalic(0, 3), types.MessageEntityTextUrl(4, 7, "https://x.com"), types.MessageEntityBlockquote(12, 3, True)]
        await replace(app, caption="new **cap** now", entities=ents)  # Telegram formatting wins: the ** stays as typed
        assert sent_caption(app) == ("new **cap** now", [("MessageEntityItalic", 0, 3), ("MessageEntityTextUrl", 4, 7), ("MessageEntityBlockquote", 12, 3)])
        assert "Formatting read from" not in said(app)[-1]
        post = await the_post(app)
        assert [e["k"] for e in post.entities] == ["italic", "url", "quote"] and post.entities[2]["c"] is True
        await app.db.close()

    asyncio.run(run())


def test_replace_reads_markdown_typed_without_the_editor(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, foreign=False)
        await replace(app, caption="**Attack on Titan** - [watch](https://t.me/x)\n> quoted")
        text, ents = sent_caption(app)
        assert text == "Attack on Titan - watch\nquoted"
        assert ents == [("MessageEntityBold", 0, 15), ("MessageEntityTextUrl", 18, 5), ("MessageEntityBlockquote", 24, 6)]
        assert "Formatting read from Markdown" in said(app)[-1]
        await app.db.close()

    asyncio.run(run())


def test_replace_reads_html_typed_without_the_editor(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, foreign=False)
        await replace(app, caption='<b>Episode 12</b> <a href="https://t.me/x">here</a> &amp; <tg-spoiler>secret</tg-spoiler>')
        text, ents = sent_caption(app)
        assert text == "Episode 12 here & secret"
        assert ents == [("MessageEntityBold", 0, 10), ("MessageEntityTextUrl", 11, 4), ("MessageEntitySpoiler", 18, 6)]
        assert "Formatting read from HTML" in said(app)[-1]
        await app.db.close()

    asyncio.run(run())


def test_markup_input_can_be_switched_off(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, foreign=False, markup_input=False)
        await replace(app, caption="**kept** <b>as</b> typed")
        assert sent_caption(app) == ("**kept** <b>as</b> typed", [])
        assert "Formatting read from" not in said(app)[-1]
        await app.db.close()

    asyncio.run(run())


def test_a_caption_that_is_too_long_is_refused_before_anything_happens(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path)
        await replace(app, caption="x" * 1025)
        assert "at most 1024 characters" in said(app)[-1]
        assert app.tg.edits == [] and user.requests == [] and chan.msgs[MID].message == "old caption"
        await app.db.close()

    asyncio.run(run())


def test_the_length_limit_counts_the_text_after_the_markup_is_read(tmp_path):
    async def run():
        app, chan, user = await make_app(tmp_path, foreign=False)
        await replace(app, caption="**" + "x" * 1020 + "**")  # 1024 characters of text; the marks don't count
        assert len(sent_caption(app)[0]) == 1020
        await app.db.close()

    asyncio.run(run())
