"""A new photo / file reaches the userbot through its own chat with the bot: no download, no upload, the thumbnail stays.

The bot sends the media to the userbot account (Telegram reuses the file it already has), the userbot finds it there and
edits the post with ITS copy. When that chat does not work, the file is downloaded and uploaded again - with its thumbnail."""
import os

import pytest
from telethon import TelegramClient, errors, types
from telethon.sessions import MemorySession

import app.userbot as userbot_module
from app.tgutil import MediaFile, fetch_media, fetch_thumb, media_ref
from app.userbot import UserbotError, UserbotRefused

from .fakes import NOW, make_msg, photo_media, rpc, video_media
from .test_userbot import FULL, channel, run
from .test_userbot_edit import ready_editor


@pytest.fixture(autouse=True)
def quick_polling(monkeypatch):
    monkeypatch.setattr(userbot_module, "_DM_WAIT", 0)  # the tests that wait for a message must not wait for real
    monkeypatch.setattr(userbot_module, "_DM_POLLS", 4)


def ref_of(media):
    """The stored reference of a photo / file the owner sent to the bot."""
    return media_ref(make_msg(1, "", media=media))


# ------------------------------------------------------------------------------------------- the quick way
def test_a_file_goes_through_the_userbots_chat_and_the_post_shows_that_same_file():
    async def go():
        bot = channel()
        bot.msgs[5].media = photo_media(10)
        new = video_media(31, thumb=True)
        ub, made, e = await ready_editor(bot)
        got = await e.media_by_reference(ref_of(new))
        assert isinstance(got, types.InputMediaDocument) and got.id.id == 31 and e.dm_problem is None
        # what the userbot presents is ITS copy: the same file, but with the access data of its own account
        assert (got.id.access_hash, got.id.file_reference) == bot._dm.known[("doc", 31)]
        assert (got.id.access_hash, got.id.file_reference) != (new.document.access_hash, new.document.file_reference)
        await e.edit(5, text="cap", entities=[], media=got)
        doc = bot.msgs[5].media.document
        assert doc.id == 31 and doc.thumbs  # the very same file, so it keeps its thumbnail
        assert made["client"].uploads == [] and bot.user_edits[-1].media is got and bot.user_edits[-1].message == "cap"
        await e.close()

    run(go())


def test_a_photo_goes_through_the_chat_too():
    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot)
        got = await e.media_by_reference(ref_of(photo_media(20)))
        assert isinstance(got, types.InputMediaPhoto) and got.id.id == 20
        await e.edit(5, text="", entities=[], media=got)
        assert bot.msgs[5].media.photo.id == 20 and made["client"].uploads == []
        await e.close()

    run(go())


def test_the_helper_message_is_sent_quietly_and_deleted_at_the_end():
    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="x", entities=[], media=await e.media_by_reference(ref_of(photo_media(20))))
        (bot_id, kw), = bot._dm.sent
        assert kw == {"silent": True}  # the account's phone does not ring for it
        assert len(bot._dm.msgs) == 1  # still there while the job runs ...
        await e.close()
        assert bot._dm.deleted == [bot_id] and bot._dm.msgs == []  # ... and gone afterwards
        assert bot.admins == {} and not ub.lock.locked()

    run(go())


def test_the_userbot_finds_its_file_among_the_other_messages_of_the_chat():
    async def go():
        bot = channel()
        bot._dm.msgs += [make_msg(901, "hello", media=None), make_msg(902, "", media=photo_media(98)), make_msg(903, "", media=video_media(97))]
        ub, made, e = await ready_editor(bot)
        got = await e.media_by_reference(ref_of(video_media(31)))
        assert got.id.id == 31  # picked by the id of the file, not by position
        await e.close()
        assert [m.id for m in bot._dm.msgs] == [901, 902, 903]  # the others were not touched

    run(go())


def test_a_file_that_reaches_the_userbot_late_is_waited_for():
    async def go():
        bot = channel()
        bot._dm.late = 2  # the first two reads find nothing yet
        ub, made, e = await ready_editor(bot)
        got = await e.media_by_reference(ref_of(photo_media(20)))
        assert got is not None and got.id.id == 20 and e.dm_problem is None
        await e.close()

    run(go())


# ------------------------------------------------------------------------------- when the chat does not work
def test_a_file_that_never_arrives_is_reported_and_the_helper_message_is_still_cleaned_up():
    async def go():
        bot = channel()
        bot._dm.late = 99
        ub, made, e = await ready_editor(bot)
        assert await e.media_by_reference(ref_of(photo_media(20))) is None
        assert e.dm_problem == "the file did not show up in the userbot's chat with the bot"
        await e.close()
        assert len(bot._dm.deleted) == 1 and bot._dm.msgs == []

    run(go())


@pytest.mark.parametrize("name", ["UserIsBlockedError", "PeerIdInvalidError", "InputUserDeactivatedError"])
def test_an_account_that_never_started_the_bot_is_told_what_to_do(name):
    async def go():
        bot = channel()
        bot._dm.error = rpc(name)
        ub, made, e = await ready_editor(bot)
        assert await e.media_by_reference(ref_of(photo_media(20))) is None
        assert "has not started the bot" in e.dm_problem and "/start" in e.dm_problem
        await e.close()
        assert bot._dm.deleted == [] and bot._dm.sent == []  # nothing was sent, so nothing is cleaned up

    run(go())


def test_any_other_refusal_of_the_send_is_named():
    async def go():
        bot = channel()
        bot._dm.error = rpc("ChatWriteForbiddenError")
        ub, made, e = await ready_editor(bot)
        assert await e.media_by_reference(ref_of(photo_media(20))) is None
        assert e.dm_problem == "Telegram would not let the bot send the file to the userbot (ChatWriteForbiddenError)"
        await e.close()

    run(go())


def test_a_chat_the_userbot_cannot_read_is_named():
    async def go():
        bot = channel()
        bot._dm.read_error = rpc("PeerIdInvalidError")
        ub, made, e = await ready_editor(bot)
        assert await e.media_by_reference(ref_of(photo_media(20))) is None
        assert e.dm_problem == "the userbot could not read its chat with the bot (PeerIdInvalidError)"
        await e.close()
        assert bot._dm.msgs == []  # the helper message is deleted all the same

    run(go())


def test_a_bot_without_a_username_cannot_be_found_and_nothing_is_sent():
    async def go():
        bot = channel()
        bot._dm.bot_user.username = None
        ub, made, e = await ready_editor(bot)
        assert await e.media_by_reference(ref_of(photo_media(20))) is None
        assert "no username" in e.dm_problem
        await e.close()
        assert bot._dm.sent == []

    run(go())


def test_a_bot_the_userbot_cannot_resolve_is_named_and_nothing_is_sent():
    async def go():
        bot = channel()
        ub, made, e = await ready_editor(bot)

        async def nothing(peer):
            raise ValueError("Cannot find any entity")

        made["client"].get_input_entity = nothing
        assert await e.media_by_reference(ref_of(photo_media(20))) is None
        assert e.dm_problem == "the userbot could not find the bot by its username"
        await e.close()
        assert bot._dm.sent == []

    run(go())


def test_a_userbot_account_the_bot_cannot_find_gets_nothing():
    async def go():
        bot = channel()
        bot.participants_visible = False  # the bot can't list the members, and has never met the account
        ub, made, e = await ready_editor(bot)
        made["client"]._join()
        bot.admins[777] = types.ChatAdminRights(edit_messages=True)  # it may edit already: no promotion, no lookup needed so far
        assert await e.media_by_reference(ref_of(photo_media(20))) is None
        assert e.dm_problem == "the bot cannot find the userbot account"
        await e.close()
        assert bot._dm.sent == []

    run(go())


def test_the_channel_comes_first_nothing_is_sent_when_the_userbot_cannot_get_in():
    async def go():
        bot = channel(rights=dict(FULL, invite=False))  # no invite link: the userbot can't join
        ub, made, e = await ready_editor(bot)
        with pytest.raises(UserbotError) as exc:
            await e.media_by_reference(ref_of(photo_media(20)))
        assert "“Edit messages of others”" in str(exc.value)
        await e.close()
        assert bot._dm.sent == []

    run(go())


# ------------------------------------------------------------------------------- when Telegram refuses the edit
def test_a_refused_edit_is_named_and_the_helper_message_is_still_deleted():
    async def go():
        bot = channel()
        bot.fail["user:EditMessageRequest"] = rpc("MessageIdInvalidError")
        ub, made, e = await ready_editor(bot)
        got = await e.media_by_reference(ref_of(photo_media(20)))
        with pytest.raises(UserbotRefused) as exc:
            await e.edit(5, text="x", entities=[], media=got)
        assert exc.value.name == "MessageIdInvalidError" and not exc.value.about_media
        assert "Telegram refused the userbot as well (MessageIdInvalidError)" in str(exc.value)
        await e.close()
        assert bot._dm.msgs == [] and bot.admins == {} and not ub.lock.locked()

    run(go())


@pytest.mark.parametrize(
    "name,about_media",
    [
        ("FileReferenceExpiredError", True), ("FileReferenceInvalidError", True), ("MediaEmptyError", True),
        ("MediaInvalidError", True), ("DocumentInvalidError", True), ("PhotoInvalidError", True),
        ("MessageIdInvalidError", False), ("ChatAdminRequiredError", False), ("MessageAuthorRequiredError", False),
    ],
)
def test_a_refusal_about_the_file_is_told_apart_from_the_others(name, about_media):
    assert UserbotRefused("x", name).about_media is about_media


def test_the_userbots_edit_with_somebody_elses_access_data_is_refused_by_the_simulation():
    """Guards the simulation itself: the bot's own reference must NOT work for the userbot (that is why the chat exists)."""

    async def go():
        bot = channel()
        new = photo_media(20)
        ub, made, e = await ready_editor(bot)
        await e.media_by_reference(ref_of(new))  # the userbot now holds its own copy ...
        bots_own = types.InputMediaPhoto(id=types.InputPhoto(20, new.photo.access_hash, new.photo.file_reference))
        with pytest.raises(UserbotRefused) as exc:  # ... but not the bot's
            await e.edit(5, text="x", entities=[], media=bots_own)
        assert exc.value.name == "FileReferenceExpiredError" and exc.value.about_media
        unknown = types.InputMediaPhoto(id=types.InputPhoto(77, 1, b"x"))
        with pytest.raises(UserbotRefused) as exc:
            await e.edit(5, text="x", entities=[], media=unknown)
        assert exc.value.name == "MediaEmptyError"
        await e.close()

    run(go())


# ----------------------------------------------------------------------------------- the slow way keeps the thumbnail
def test_an_uploaded_file_takes_its_thumbnail_along(tmp_path):
    async def go():
        bot = channel()
        video, thumb = tmp_path / "Episode 12.mkv", tmp_path / "photo_1.jpg"
        video.write_bytes(b"VIDEO BYTES")
        thumb.write_bytes(b"THE THUMBNAIL")
        attrs = [types.DocumentAttributeFilename("Episode 12.mkv"), types.DocumentAttributeVideo(5.0, 320, 240)]
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="", entities=[], media=MediaFile(str(video), "video", "Episode 12.mkv", "video/x-matroska", attrs, str(thumb)))
        await e.close()
        sent = bot.user_edits[-1].media
        assert isinstance(sent, types.InputMediaUploadedDocument) and sent.thumb is not None
        assert made["client"].uploads == [(str(video), "Episode 12.mkv", 11), (str(thumb), None, 13)]
        doc = bot.msgs[5].media.document
        assert doc.thumbs and bot._media_data[("thumb", doc.id)] == b"THE THUMBNAIL"

    run(go())


def test_a_thumbnail_that_cannot_be_uploaded_does_not_stop_the_edit(tmp_path):
    async def go():
        bot = channel()
        video = tmp_path / "x.mkv"
        video.write_bytes(b"V")
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="", entities=[], media=MediaFile(str(video), "video", "x.mkv", thumb_path=str(tmp_path / "gone.jpg")))
        await e.close()
        assert bot.user_edits[-1].media.thumb is None and bot.msgs[5].media.document.thumbs is None  # it went without

    run(go())


def test_a_file_without_a_thumbnail_is_uploaded_without_one(tmp_path):
    async def go():
        bot = channel()
        video = tmp_path / "x.mkv"
        video.write_bytes(b"V")
        ub, made, e = await ready_editor(bot)
        await e.edit(5, text="", entities=[], media=MediaFile(str(video), "document", "x.mkv"))
        await e.close()
        assert bot.user_edits[-1].media.thumb is None and len(made["client"].uploads) == 1

    run(go())


# ---------------------------------------------------------------------------------------- downloading the thumbnail
def patch_download(monkeypatch, wanted_sizes):
    """Telethon's real download_media runs; only the transfer of the bytes is replaced (it records what was asked for)."""

    async def fake_download(self, input_location, file, **kw):
        wanted_sizes.append(getattr(input_location, "thumb_size", None))
        with open(file, "wb") as f:
            f.write(b"THUMB" if getattr(input_location, "thumb_size", "") else b"THE BYTES")
        return file

    monkeypatch.setattr(TelegramClient, "_download_file", fake_download)


def doc_ref(thumbs, attrs=None):
    attrs = attrs or [types.DocumentAttributeFilename("Episode 12.mkv"), types.DocumentAttributeVideo(5.0, 320, 240)]
    doc = types.Document(21, 7, b"fr", NOW, "video/x-matroska", 1000, 2, attrs, thumbs=thumbs)
    return ref_of(types.MessageMediaDocument(document=doc))


def test_the_largest_thumbnail_is_downloaded_with_the_file(tmp_path, monkeypatch):
    asked = []
    patch_download(monkeypatch, asked)
    ref = doc_ref([types.PhotoStrippedSize("i", b"\x01\x28\x28"), types.PhotoSize("s", 100, 56, 900), types.PhotoSize("m", 320, 180, 4000)])

    async def go():
        client = TelegramClient(MemorySession(), 1, "h")
        got = await fetch_media(client, ref, "video", str(tmp_path))
        assert open(got.path, "rb").read() == b"THE BYTES" and got.name == "Episode 12.mkv"
        assert got.thumb_path and got.thumb_path.endswith(".jpg") and os.path.dirname(got.thumb_path) != os.path.dirname(got.path)
        assert open(got.thumb_path, "rb").read() == b"THUMB"
        assert asked == ["", "m"]  # the file itself, then the biggest picture of the file

    run(go())


def test_a_file_without_a_thumbnail_has_none(tmp_path, monkeypatch):
    asked = []
    patch_download(monkeypatch, asked)

    async def go():
        client = TelegramClient(MemorySession(), 1, "h")
        got = await fetch_media(client, doc_ref(None), "video", str(tmp_path))
        assert got.thumb_path is None and os.path.isfile(got.path) and asked == [""]

    run(go())


def test_a_file_that_only_has_the_tiny_inline_thumbnail_still_gets_one(tmp_path, monkeypatch):
    asked = []
    patch_download(monkeypatch, asked)
    stripped = types.PhotoStrippedSize("i", b"\x01\x28\x28\xa2\x8a\x28\x03")

    async def go():
        client = TelegramClient(MemorySession(), 1, "h")
        got = await fetch_media(client, doc_ref([stripped]), "video", str(tmp_path))
        assert got.thumb_path and os.path.getsize(got.thumb_path) > 0  # Telethon turns the inline bytes into a JPEG itself
        assert asked == [""]  # no download needed: the picture was inside the message

    run(go())


def test_a_thumbnail_that_cannot_be_downloaded_is_left_out_and_the_file_is_kept(tmp_path, monkeypatch):
    async def fake_download(self, input_location, file, **kw):
        if getattr(input_location, "thumb_size", ""):
            raise errors.FileReferenceExpiredError(request=None)
        with open(file, "wb") as f:
            f.write(b"THE BYTES")
        return file

    monkeypatch.setattr(TelegramClient, "_download_file", fake_download)
    ref = doc_ref([types.PhotoSize("m", 320, 180, 4000)])

    async def go():
        client = TelegramClient(MemorySession(), 1, "h")
        got = await fetch_media(client, ref, "video", str(tmp_path))
        assert got.thumb_path is None and open(got.path, "rb").read() == b"THE BYTES"

    run(go())


def test_a_photo_is_fetched_without_a_thumbnail_step(tmp_path, monkeypatch):
    asked = []
    patch_download(monkeypatch, asked)

    async def go():
        client = TelegramClient(MemorySession(), 1, "h")
        got = await fetch_media(client, ref_of(photo_media(11)), "photo", str(tmp_path))
        assert got.thumb_path is None and len(asked) == 1  # the photo itself, nothing else

    run(go())


def test_a_client_that_cannot_give_thumbnails_is_not_a_problem(tmp_path):
    class Plain:  # a download_media that knows nothing of thumbnails
        async def download_media(self, media, file=None):
            path = str(file) + "x.bin"
            with open(path, "wb") as f:
                f.write(b"B")
            return path

    async def go():
        got = await fetch_media(Plain(), doc_ref([types.PhotoSize("m", 320, 180, 4000)]), "document", str(tmp_path))
        assert got.thumb_path is None and os.path.isfile(got.path)
        assert await fetch_thumb(Plain(), doc_ref(None), str(tmp_path)) is None

    run(go())
