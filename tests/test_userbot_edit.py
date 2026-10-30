"""The userbot as an editor: it edits what Telegram won't let the bot edit (text, caption, a new photo or file)."""
import asyncio
import os

import pytest
from telethon import TelegramClient, types
from telethon.sessions import MemorySession

from app.tgutil import MediaFile, fetch_media, media_ref
from app.userbot import UserbotError, edit_problem_text

from .fakes import NOW, make_msg, markup, photo_media, rpc, url_btn
from .test_userbot import FULL, channel, ch_row, make_userbot, run


async def ready_editor(bot, **kw):
    ub, made = make_userbot(bot, **kw)
    assert await ub.connect()
    return ub, made, ub.editor(ch_row(bot))


def only_right(rights, name):
    return getattr(rights, name) and not any(v for k, v in rights.to_dict().items() if k not in ("_", name))


# ------------------------------------------------------------------------------------------ text
def test_a_text_edit_joins_gets_only_the_edit_right_and_gives_it_back():
    async def go():
        bot = channel()
        bot.msgs[5].reply_markup = markup([url_btn("Go", "https://t.me/x")])
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="new text", entities=[types.MessageEntityBold(0, 3)])
        m = bot.msgs[5]
        assert m.message == "new text" and [type(x).__name__ for x in m.entities] == ["MessageEntityBold"]
        assert m.reply_markup is None  # a user account sends no keyboard, so Telegram drops it
        assert only_right(bot.admins[777], "edit_messages")
        assert e.notes == [
            "made a one-time invite link for the userbot",
            "the userbot joined the channel",
            "made the userbot an admin with only “Edit messages of others”",
        ]
        await e.close()
        assert bot.admins == {} and not ub.lock.locked()
        assert e.notes[-1] == "took the admin right away from the userbot again"
        req = made["client"].requests[-1]
        assert type(req).__name__ == "EditMessageRequest" and req.media is None and req.reply_markup is None

    run(go())


def test_the_link_preview_choice_is_sent_for_text_posts_only():
    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="a https://x.com", entities=[], preview=False)
        await e.edit(6, text="b", entities=[])
        await e.close()
        first, second = bot.user_edits
        assert first.no_webpage is True and not second.no_webpage

    run(go())


# ----------------------------------------------------------------------------------------- media
def test_a_new_photo_is_uploaded_and_replaces_the_old_one(tmp_path):
    async def go():
        bot = channel()
        bot.msgs[5].media = photo_media(10)
        path = tmp_path / "new.jpg"
        path.write_bytes(b"NEW PHOTO")
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="cap", entities=[], media=MediaFile(str(path), "photo", "new.jpg"))
        await e.close()
        m = bot.msgs[5]
        assert isinstance(m.media, types.MessageMediaPhoto) and m.media.photo.id != 10
        assert bot._media_data[("photo", m.media.photo.id)] == b"NEW PHOTO" and m.message == "cap"
        assert made["client"].uploads == [(str(path), "new.jpg", 9)]
        req = bot.user_edits[-1]
        assert isinstance(req.media, types.InputMediaUploadedPhoto) and req.message == "cap"

    run(go())


@pytest.mark.parametrize("kind,forced", [("video", None), ("animation", None), ("audio", None), ("document", True)])
def test_a_file_keeps_its_name_type_and_attributes(tmp_path, kind, forced):
    async def go():
        bot = channel()
        path = tmp_path / "Episode 12.mkv"
        path.write_bytes(b"VIDEO BYTES")
        attrs = [types.DocumentAttributeFilename("Episode 12.mkv"), types.DocumentAttributeVideo(5.0, 320, 240)]
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="", entities=[], media=MediaFile(str(path), kind, "Episode 12.mkv", "video/x-matroska", attrs))
        await e.close()
        req = bot.user_edits[-1]
        assert isinstance(req.media, types.InputMediaUploadedDocument)
        assert req.media.mime_type == "video/x-matroska" and req.media.attributes == attrs and req.media.force_file == forced
        doc = bot.msgs[5].media.document
        assert doc.mime_type == "video/x-matroska" and doc.attributes == attrs and doc.size == 11

    run(go())


def test_a_file_without_a_known_type_is_uploaded_as_a_plain_file(tmp_path):
    async def go():
        bot = channel()
        path = tmp_path / "x.bin"
        path.write_bytes(b"1")
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="", entities=[], media=MediaFile(str(path), "document", "x.bin"))
        await e.close()
        assert bot.user_edits[-1].media.mime_type == "application/octet-stream"

    run(go())


# ------------------------------------------------------------------------------------------ rights
def test_rights_the_userbot_already_has_are_kept_and_given_back():
    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot)
        made["client"]._join()
        bot.admins[777] = types.ChatAdminRights(delete_messages=True)  # e.g. left over from USERBOT_KEEP_ADMIN
        await e.edit(5, text="x", entities=[])
        assert bot.admins[777].delete_messages and bot.admins[777].edit_messages
        assert "gave the userbot the “Edit messages of others” right on top of the ones it had" in e.notes
        await e.close()
        assert bot.admins[777].delete_messages and not bot.admins[777].edit_messages
        assert e.notes[-1] == "put the userbot's admin rights back as they were"

    run(go())


def test_a_userbot_that_may_already_edit_is_left_alone():
    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot)
        made["client"]._join()
        bot.admins[777] = types.ChatAdminRights(edit_messages=True)
        await e.edit(5, text="x", entities=[])
        await e.close()
        assert e.notes == [] and bot.admins[777].edit_messages
        assert not [r for r in bot.requests if type(r).__name__ == "EditAdminRequest"]

    run(go())


def test_keep_admin_keeps_the_edit_right():
    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot, keep_admin=True)
        await e.edit(5, text="x", entities=[])
        await e.close()
        assert only_right(bot.admins[777], "edit_messages")
        assert not any("took the admin right away" in n for n in e.notes)

    run(go())


def test_without_the_add_admins_right_the_owner_is_told_what_to_do():
    async def go():
        bot = channel(rights=dict(FULL, add_admins=False))
        ub, made, e = await ready_editor(bot)
        made["client"]._join()
        with pytest.raises(UserbotError) as exc:
            await e.edit(5, text="x", entities=[])
        text = str(exc.value)
        assert "edit other people's posts" in text and "Add new admins" in text and "“Edit messages of others”" in text
        await e.close()
        assert not ub.lock.locked()

    run(go())


def test_an_invite_problem_names_the_right_to_give():
    async def go():
        bot = channel(rights=dict(FULL, invite=False))  # no link can be made and the channel has none
        ub, made, e = await ready_editor(bot)
        with pytest.raises(UserbotError) as exc:
            await e.edit(5, text="x", entities=[])
        assert "“Edit messages of others”" in str(exc.value) and "Delete messages" not in str(exc.value)
        await e.close()

    run(go())


# ---------------------------------------------------------------------------------------- failures
def test_a_refused_edit_is_reported_with_the_name_and_cleans_up():
    async def go():
        bot = channel()
        bot.fail["user:EditMessageRequest"] = rpc("MessageIdInvalidError")
        ub, made, e = await ready_editor(bot)
        with pytest.raises(UserbotError) as exc:
            await e.edit(5, text="x", entities=[])
        assert "Telegram refused the userbot as well (MessageIdInvalidError)" in str(exc.value)
        await e.close()
        assert bot.admins == {} and not ub.lock.locked()

    run(go())


def test_a_missing_right_in_telegrams_eyes_is_explained():
    async def go():
        bot = channel()
        bot.fail["user:EditMessageRequest"] = rpc("MessageAuthorRequiredError")
        ub, made, e = await ready_editor(bot)
        with pytest.raises(UserbotError) as exc:
            await e.edit(5, text="x", entities=[])
        assert "MessageAuthorRequiredError" in str(exc.value) and "Edit messages of others" in str(exc.value)
        await e.close()

    run(go())


def test_nothing_to_change_is_not_an_error():
    async def go():
        bot = channel()
        bot.msgs[5].message = "same"
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="same", entities=[])
        await e.close()
        assert bot.msgs[5].message == "same" and bot.user_edits == []

    run(go())


def test_an_editor_without_a_connected_userbot_says_so():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot, session="")
        assert await ub.editor_for(ch_row(bot)) is None
        with pytest.raises(UserbotError, match="not connected"):
            await ub.editor(ch_row(bot)).edit(5, text="x", entities=[])

    run(go())


def test_editor_for_connects_the_userbot_when_needed():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot)
        assert not ub.ready
        e = await ub.editor_for(ch_row(bot))
        assert e is not None and ub.ready
        await e.close()

    run(go())


def test_an_edit_and_a_clean_up_do_not_overlap():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot)
        assert await ub.connect()
        e, d = ub.editor(ch_row(bot)), ub.deleter(ch_row(bot))
        await e.edit(5, text="x", entities=[])
        task = asyncio.ensure_future(d.delete([1]))
        await asyncio.sleep(0.05)
        assert not task.done() and 1 in bot.msgs  # it waits until the editor lets go
        await e.close()
        await asyncio.wait_for(task, 5)
        await d.close()
        assert 1 not in bot.msgs and bot.admins == {}

    run(go())


def test_edit_problem_text():
    ready = type("U", (), {"enabled": True, "ready": True, "last_error": None})()
    off = type("U", (), {"enabled": False, "ready": False, "last_error": None})()
    down = type("U", (), {"enabled": True, "ready": False, "last_error": "the session was revoked"})()
    assert "see /userbot" in edit_problem_text(None) and "see /userbot" in edit_problem_text(off)
    assert "the session was revoked" in edit_problem_text(down)
    assert edit_problem_text(ready, fallback_error="boom") == "The userbot could not do it either: boom"
    assert edit_problem_text(ready) == ""


# ------------------------------------------------------------------------------ the bot's download
def test_fetch_media_names_a_file_like_the_original_and_keeps_its_attributes(tmp_path, monkeypatch):
    async def fake_download(self, input_location, file, **kw):
        with open(file, "wb") as f:
            f.write(b"THE BYTES")
        return file

    monkeypatch.setattr(TelegramClient, "_download_file", fake_download)
    attrs = [types.DocumentAttributeFilename("Episode 12.mkv"), types.DocumentAttributeVideo(5.0, 320, 240)]
    doc = types.Document(21, 7, b"fr", NOW, "video/x-matroska", 1000, 2, attrs)
    ref = media_ref(make_msg(1, "", media=types.MessageMediaDocument(document=doc)))

    async def go():
        client = TelegramClient(MemorySession(), 1, "h")
        got = await fetch_media(client, ref, "video", str(tmp_path))
        assert os.path.basename(got.path) == "Episode 12.mkv" and got.name == "Episode 12.mkv"
        assert open(got.path, "rb").read() == b"THE BYTES"
        assert got.kind == "video" and got.mime_type == "video/x-matroska"
        assert [bytes(a) for a in got.attributes] == [bytes(a) for a in attrs]  # (stored and read back: same on the wire)
        photo = media_ref(make_msg(2, "", media=photo_media(11)))
        pic = await fetch_media(client, photo, "photo", str(tmp_path))
        assert pic.path.endswith(".jpg") and pic.mime_type is None and pic.attributes == []

    asyncio.run(go())


def test_fetch_media_says_so_when_telegram_gives_nothing(tmp_path):
    class Silent:
        async def download_media(self, media, file=None):
            return None

    ref = media_ref(make_msg(2, "", media=photo_media(11)))
    with pytest.raises(ValueError):
        asyncio.run(fetch_media(Silent(), ref, "photo", str(tmp_path)))
