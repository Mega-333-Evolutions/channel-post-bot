"""Automatic publishing: the autorename bot's message in the communication chat -> a post in the channel it names,
made from the owner's text and button templates (/autopublish)."""
import asyncio
import copy
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from telethon import events, types

from app.dbpublish import (
    DEFAULT_BUTTON,
    DEFAULT_TEXT,
    POLL_SECONDS,
    AutoPublisher,
    Notice,
    Templates,
    channel_id_of,
    chat_number,
    file_link,
    fill,
    fill_text,
    load_templates,
    marked_chat_id,
    parse_notice,
    render_post,
    reset_templates,
    save_templates,
    share_button,
    unknown_variables,
    uses,
)
from app.richtext import convert
from app.tgutil import button_url, de_entities, ser_entities
from app.userbot import Userbot, UserbotRefused

from .fakes import NOW, FakeUserClient, cb_btn, markup, rpc, url_btn
from .harness import App, norm

DB, DBM = 1, -1000000000001  # the Database Channel: its id in the database, and as Telegram apps write it
BLACK, BLACKM = 2, -1000000000002  # the channel of the series
COMM = -1000000000099  # the communication chat
AUTO = 8926792949  # the autorename bot
FILE = "Black Clover - S2 E05.mkv"
FILE_LINK = "https://t.me/filestore_bot?start=Zm9v"
SHARE = "https://t.me/share/url?url=" + quote(FILE_LINK, safe="")  # what a 🔁 Share URL button usually holds
DEFAULT_POST_TEXT = "Episode 05\n\nClick the button below to download 👇"


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------------ fakes
class Clock:
    """Time that only moves when the publisher sleeps."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


class Comm:
    """A message in the communication chat; `.replies` are what the bot answered."""

    def __init__(self, text, mid=1, chat=COMM, sender=AUTO):
        self.raw_text, self.id, self.chat_id, self.sender_id = text, mid, chat, sender
        self.replies = []
        self.kw = {}

    async def reply(self, text, **kw):
        self.replies.append(text)
        self.kw = kw


class FakeReader:
    """What the userbot's reader gives: the posts the userbot account sees."""

    def __init__(self, clock, msgs=None, fail=None):
        self.clock, self.msgs, self.fail = clock, msgs or {}, fail
        self.times, self.closed = [], 0

    async def read(self, message_id=None, limit=3):
        self.times.append(self.clock.t)
        if self.fail is not None:
            raise self.fail
        if message_id:
            return [self.msgs[message_id]] if message_id in self.msgs else []
        return sorted(self.msgs.values(), key=lambda m: m.id, reverse=True)[:limit]

    async def close(self):
        self.closed += 1


class FakeUserbot:
    def __init__(self, reader=None, *, enabled=True, last_error=""):
        self.reader, self.enabled, self.last_error, self.asked = reader, enabled, last_error, 0
        self.ready = reader is not None

    async def reader_for(self, ch):
        self.asked += 1
        return self.reader


def file_media(name=FILE, did=31):
    doc = types.Document(did, did * 7, b"fr", NOW, "video/x-matroska", 1000, 2, [types.DocumentAttributeFilename(name)])
    return types.MessageMediaDocument(document=doc)


def keyboard(*buttons):
    """A keyboard with all the buttons in one row."""
    return markup(list(buttons))


def share_markup(link=SHARE):
    return keyboard(url_btn("🔁 Share URL", link))


def file_post(app, name=FILE, *, buttons=True, link=SHARE, hide=False):
    """A file in the Database Channel (what the autorename bot copied there), with the Share URL button the file store adds."""
    return app.tg._new("", None, share_markup(link) if buttons else None, media=file_media(name), hide=hide)


def notice(*, episode="05", channel=BLACKM, file=FILE, post=None, ref="r1"):
    lines = ["File uploaded to the Database Channel", ""]
    if episode is not None:
        lines.append(f"Episode: {episode}")
    if channel is not None:
        lines.append(f"Channel: {channel}")
    if file:
        lines.append(f"File: {file}")
    if post is not None:
        lines.append(f"Post: {post}")
    if ref:
        lines.append(f"Ref: {ref}")
    return "\n".join(lines)


async def make_app(tmp_path, **cfg):
    cfg.setdefault("db_channel", DBM)
    cfg.setdefault("comm_chat", COMM)
    app = App(tmp_path, edit_delay=0, **cfg)
    await app.start()
    await app.db.save_channel(DB, 5, "Database Channel", None, 1)
    await app.db.save_channel(BLACK, 6, "Black Clover English", "blackclover", 1)
    app.clock = Clock()
    app.ctx.publisher = AutoPublisher(app.ctx, sleep=app.clock.sleep, clock=app.clock.now)
    return app


async def handle(app, text, **kw):
    ev = Comm(text, **kw)
    await app.ctx.publisher.handle(ev)
    return ev


def last_post(app):
    """(peer, text, entities, button) of the last post the bot sent."""
    kind, peer, text, kw = app.tg.sent[-1]
    btn = kw["buttons"].rows[0].buttons[0]
    return peer, text, kw.get("formatting_entities"), btn


# ---------------------------------------------------------------------------------------------- the notice
def test_the_notice_is_read_line_by_line():
    n = parse_notice(
        "File uploaded to the Database Channel\n\nEpisode: 05\nChannel: -1001234567890\n"
        "File: Black Clover - S2 E05.mkv\nPost: 1234\nRef: 3f9a1c"
    )
    assert n == Notice(file=FILE, episode="05", channel=-1001234567890, channel_raw="-1001234567890", post_id=1234, ref="3f9a1c")


def test_the_notice_needs_only_its_first_line_and_forgives_capitals_and_spacing():
    n = parse_notice("FILE UPLOADED TO THE DATABASE CHANNEL\nepisode:5\n  CHANNEL :  -1009 \n")
    assert n.episode == "5" and n.channel == -1009 and n.post_id is None and n.file == "" and n.ref == ""
    assert parse_notice("file  uploaded   to the database channel") == Notice()


def test_the_zeros_of_the_episode_number_stay():
    assert parse_notice("File uploaded to the Database Channel\nEpisode: 05").episode == "05"
    assert parse_notice("File uploaded to the Database Channel\nEpisode: 1147").episode == "1147"
    assert parse_notice("File uploaded to the Database Channel\nEpisode: 004").episode == "004"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("-1001234567890", -1001234567890),
        ("1234567890", 1234567890),
        (" -1001234567890 (Black Clover) ", -1001234567890),
        ("https://t.me/c/1234567890/5", -1001234567890),
        ("t.me/c/1234567890", -1001234567890),
        ("Black Clover", None),
        ("@blackclover", None),
        ("", None),
        (None, None),
    ],
)
def test_a_channel_can_be_written_in_several_ways(raw, expected):
    assert chat_number(raw) == expected


def test_a_channel_that_is_no_id_is_kept_as_written():
    n = parse_notice("File uploaded to the Database Channel\nChannel: Black Clover")
    assert n.channel is None and n.channel_raw == "Black Clover"


@pytest.mark.parametrize("text", ["hello", "Message received", "✅ Post published successfully", "❌ Publication failed: x", "", None])
def test_other_messages_are_not_notices(text):
    assert parse_notice(text) is None


def test_ids_as_telegram_apps_write_them():
    assert channel_id_of(-1001234567890) == 1234567890
    assert channel_id_of(1234567890) == 1234567890
    assert marked_chat_id(1234567890) == -1001234567890
    assert marked_chat_id(-1001234567890) == -1001234567890


# ----------------------------------------------------------------------------------------- the file link
def test_the_share_sheet_link_is_taken_off_the_file_link():
    assert file_link(SHARE) == FILE_LINK
    assert file_link("https://t.me/share/url?url=" + quote(FILE_LINK, safe="") + "&text=Hello") == FILE_LINK
    assert file_link("https://t.me/share/url?url=https://t.me/bot?start=abc") == "https://t.me/bot?start=abc"
    assert file_link("https://telegram.me/share?url=" + quote(FILE_LINK, safe="")) == FILE_LINK
    assert file_link(FILE_LINK) == FILE_LINK  # a link that is none of that stays as it is
    assert file_link("") == ""
    for empty in ("https://t.me/share/url?url=notalink", "https://t.me/share/url?url=", "https://t.me/share?text=hi"):
        assert file_link(empty) == ""  # a share sheet that holds no link of its own: nothing to download from it


def test_the_share_url_button_is_found_by_its_name():
    assert share_button(keyboard(cb_btn("Menu", "m"), url_btn("🔁 Share URL", SHARE))) == ("🔁 Share URL", SHARE)
    assert share_button(keyboard(url_btn("share url", SHARE))) == ("share url", SHARE)
    assert share_button(keyboard(url_btn("Download", FILE_LINK))) is None
    assert share_button(keyboard(cb_btn("🔁 Share URL", "x"))) is None  # a button that is no link
    assert share_button(None) is None


# --------------------------------------------------------------------------------------------- templates
def test_the_default_post_is_the_one_the_owner_described():
    text, entities, label = render_post(Templates(), {"episode": "05", "channel": "Black Clover English"})
    assert text == DEFAULT_POST_TEXT == "Episode 05\n\nClick the button below to download 👇"
    assert entities == [] and label == "Download Episode 05"
    assert DEFAULT_TEXT == "Episode {episode}\n\nClick the button below to download 👇" and DEFAULT_BUTTON == "Download Episode {episode}"


def test_a_variable_can_be_written_in_every_spelling():
    values = {"episode": "07", "channel": "Show"}
    assert fill("{episode}|{episode number}|{Episode_Number}|{ episode }|{EPISODE NUM}", values) == "07|07|07|07|07"
    assert fill("{channel}|{channel name}|{Channel_Title}", values) == "Show|Show|Show"
    assert fill("no variables {here}", values) == "no variables {here}"
    assert uses("Download {episode number}", "episode") and not uses("Hi {channel}", "episode") and not uses("", "episode")


def test_words_in_braces_that_are_no_variables_are_listed():
    assert unknown_variables("{ep} {episode} {foo bar} {channel name} {}") == ["{ep}", "{foo bar}", "{}"]
    assert unknown_variables("Episode {episode}") == [] and unknown_variables(None) == []


def test_formatting_stays_on_the_words_it_covered():
    text, ents, _how = convert("**Episode {episode}** is out\n__{channel}__")
    new_text, new_ents = fill_text(text, ents, {"episode": "05", "channel": "Black Clover"})
    assert new_text == "Episode 05 is out\nBlack Clover"
    bold, under = de_entities(new_ents)
    assert isinstance(bold, types.MessageEntityBold) and (bold.offset, bold.length) == (0, 10)
    assert isinstance(under, types.MessageEntityUnderline) and (under.offset, under.length) == (18, 12)


def test_offsets_count_emoji_as_telegram_does():
    text = "👇 Episode {episode} 👇"
    ents = ser_entities([types.MessageEntityBold(3, 17), types.MessageEntityItalic(21, 2)])
    new_text, new_ents = fill_text(text, ents, {"episode": "05"})
    assert new_text == "👇 Episode 05 👇"
    bold, italic = de_entities(new_ents)
    assert (bold.offset, bold.length) == (3, 10) and (italic.offset, italic.length) == (14, 2)


def test_a_value_with_an_emoji_in_it_moves_what_follows_by_its_utf16_length():
    text = "{channel} now"
    ents = ser_entities([types.MessageEntityBold(10, 3)])
    new_text, new_ents = fill_text(text, ents, {"channel": "Show 🔥"})
    assert new_text == "Show 🔥 now"
    (bold,) = de_entities(new_ents)
    assert (bold.offset, bold.length) == (8, 3)
    assert new_text.encode("utf-16-le")[16:22].decode("utf-16-le") == "now"  # it still covers "now"


def test_the_templates_survive_in_the_database(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        t = await load_templates(app.db)
        assert (t.text, t.button, t.custom_text, t.custom_button) == (DEFAULT_TEXT, DEFAULT_BUTTON, False, False)
        await save_templates(app.db, text="Hi {episode}", entities=[{"k": "bold", "o": 0, "l": 2}])
        await save_templates(app.db, button="Get {episode}")
        t = await load_templates(app.db)
        assert (t.text, t.button, t.custom_text, t.custom_button) == ("Hi {episode}", "Get {episode}", True, True)
        assert t.entities == [{"k": "bold", "o": 0, "l": 2}]
        await reset_templates(app.db, text=True)
        t = await load_templates(app.db)
        assert (t.text, t.entities, t.button, t.custom_text, t.custom_button) == (DEFAULT_TEXT, [], "Get {episode}", False, True)
        await reset_templates(app.db, button=True)
        assert await app.db.get_setting("autopublish") is None  # nothing left of it
        await app.db.close()

    run(go())


# ------------------------------------------------------------------------------------------ the happy path
def test_a_post_is_made_in_the_channel_the_message_names(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)
        ev = await handle(app, notice(post=post.id))
        assert len(ev.replies) == 2
        assert ev.replies[0].startswith("📥 Message received") and "Ref: r1" in ev.replies[0]
        done = ev.replies[1]
        assert done.startswith("✅ Post published successfully")
        for part in ("Channel: Black Clover English", "Episode: 05", f"File: {FILE}", "Ref: r1", "https://t.me/blackclover/"):
            assert part in done, done
        assert ev.kw == {"parse_mode": None, "link_preview": False}
        peer, text, entities, btn = last_post(app)
        assert peer.channel_id == BLACK
        assert text == DEFAULT_POST_TEXT and not entities
        assert btn.text == "Download Episode 05" and button_url(btn) == FILE_LINK  # the file link, not the share sheet
        (row,) = await app.db.latest_sent(BLACK)
        assert (row.status, row.source, row.text) == ("sent", "bot", DEFAULT_POST_TEXT) and row.message_id
        assert f"Post: https://t.me/blackclover/{row.message_id}" in done
        assert row.buttons == [[{"t": "Download Episode 05", "u": FILE_LINK}]] and row.link_preview is False
        assert not app.tg.requests  # nothing was changed in the Database Channel
        await app.db.close()

    run(go())


def test_the_post_goes_where_the_message_says_not_where_the_file_name_points(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.db.save_channel(3, 7, "One Piece English Dub", "onepiece", 1)
        post = file_post(app, "Black Clover - S2 E05.mkv")
        ev = await handle(app, notice(channel=-1000000000003, post=post.id))
        assert ev.replies[-1].startswith("✅") and "Channel: One Piece English Dub" in ev.replies[-1]
        assert last_post(app)[0].channel_id == 3
        await app.db.close()

    run(go())


def test_a_bare_channel_id_works_too(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)
        ev = await handle(app, notice(channel=BLACK, post=post.id))
        assert ev.replies[-1].startswith("✅") and last_post(app)[0].channel_id == BLACK
        await app.db.close()

    run(go())


def test_the_owners_own_templates_are_used(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopublish")
        await app.press("dpb:t")
        await app.text("**Episode {episode}** of {channel}\n\nDownload it below 👇")
        await app.press("dpb:b")
        await app.text("📥 Episode {episode number}")
        post = file_post(app)
        ev = await handle(app, notice(episode="12", post=post.id))
        assert ev.replies[-1].startswith("✅")
        _peer, text, entities, btn = last_post(app)
        assert text == "Episode 12 of Black Clover English\n\nDownload it below 👇"
        (bold,) = entities
        assert isinstance(bold, types.MessageEntityBold) and (bold.offset, bold.length) == (0, 10)
        assert btn.text == "📥 Episode 12" and button_url(btn) == FILE_LINK
        await app.db.close()

    run(go())


# ------------------------------------------------------------------------------------ waiting for the button
def test_it_waits_for_the_button_and_posts_when_it_shows_up(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app, buttons=False)  # the file store has not added its button yet
        reads, orig = [], app.tg.get_messages

        async def watching(peer, ids=None):
            reads.append(app.clock.t)
            if app.clock.t >= 12:  # "a few seconds later"
                app.tg._set_markup(post, share_markup())
            return await orig(peer, ids=ids)

        app.tg.get_messages = watching
        ev = await handle(app, notice(post=post.id))
        assert ev.replies[-1].startswith("✅ Post published successfully")
        assert reads[0] == 0 and reads[-1] >= 12 and len(reads) >= 5
        assert set(app.clock.sleeps) == {POLL_SECONDS}
        await app.db.close()

    run(go())


def test_no_button_is_reported_only_after_at_least_ten_seconds(tmp_path):
    async def go():
        app = await make_app(tmp_path, share_url_wait=10.0)
        post = file_post(app, buttons=False)
        ev = await handle(app, notice(post=post.id))
        failed = ev.replies[-1]
        assert failed.startswith("❌ Publication failed: No 🔁 Share URL button was found")
        assert "after waiting 10 seconds" in failed and "through the bot (no userbot is set up or reachable)" in failed
        assert f"File: {FILE}" in failed and "Ref: r1" in failed
        assert app.clock.t >= 10.0  # it did look for the whole time
        assert not app.tg.sent  # and posted nothing
        await app.db.close()

    run(go())


def test_a_wait_shorter_than_ten_seconds_is_never_used(tmp_path):
    async def go():
        app = await make_app(tmp_path, share_url_wait=2.0)  # (the setting itself is raised to 10 as well: see the config tests)
        post = file_post(app, buttons=False)
        ev = await handle(app, notice(post=post.id))
        assert "after waiting 10 seconds" in ev.replies[-1] and app.clock.t >= 10.0
        await app.db.close()

    run(go())


def test_the_button_is_taken_at_once_when_it_is_already_there(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)
        await handle(app, notice(post=post.id))
        assert app.clock.sleeps == []  # nothing to wait for
        await app.db.close()

    run(go())


# ---------------------------------------------------------------------------------------- the userbot looks
def test_the_userbot_looks_when_the_bot_sees_no_button(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app, hide=True)  # a post of another bot: its keyboard is not shown to this bot
        seen_by_userbot = copy.copy(post)
        seen_by_userbot.reply_markup = share_markup()
        reader = FakeReader(app.clock, {post.id: seen_by_userbot})
        ub = app.ctx.userbot = FakeUserbot(reader)
        ev = await handle(app, notice(post=post.id))
        assert ev.replies[-1].startswith("✅ Post published successfully")
        assert "The Share URL button was read through the userbot." in ev.replies[-1]
        assert ub.asked == 1 and reader.closed == 1
        assert reader.times and reader.times[0] >= 10.0  # the bot had its ten seconds on its own first
        assert button_url(last_post(app)[3]) == FILE_LINK
        await app.db.close()

    run(go())


def test_the_userbot_is_asked_at_once_when_the_bot_cannot_read_the_post(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app, hide=True)
        seen_by_userbot = copy.copy(post)
        seen_by_userbot.reply_markup = share_markup()
        app.tg.fail["GetMessagesRequest"] = rpc("ChannelPrivateError")  # the bot may not read that channel at all
        reader = FakeReader(app.clock, {post.id: seen_by_userbot})
        app.ctx.userbot = FakeUserbot(reader)
        ev = await handle(app, notice(post=post.id))
        assert ev.replies[-1].startswith("✅") and reader.times == [0.0] and app.clock.sleeps == []
        await app.db.close()

    run(go())


def test_when_nobody_can_read_the_post_both_reasons_are_given(tmp_path):
    async def go():
        app = await make_app(tmp_path, share_url_wait=10.0)
        post = file_post(app)
        app.tg.fail["GetMessagesRequest"] = rpc("ChannelPrivateError")
        app.ctx.userbot = FakeUserbot(None, enabled=True, last_error="not logged in any more")
        ev = await handle(app, notice(post=post.id))
        failed = ev.replies[-1]
        assert failed.startswith("❌ Publication failed: I could not read the post of the Database Channel")
        assert "Telegram refused the bot (ChannelPrivateError)" in failed and "the userbot is not connected: not logged in any more" in failed
        await app.db.close()

    run(go())


def test_a_userbot_that_cannot_read_is_reported_after_the_wait(tmp_path):
    async def go():
        app = await make_app(tmp_path, share_url_wait=10.0)
        post = file_post(app, buttons=False)
        refused = UserbotRefused("Telegram refused the userbot (ChannelPrivateError).", "ChannelPrivateError")
        reader = FakeReader(app.clock, fail=refused)
        ub = app.ctx.userbot = FakeUserbot(reader)
        ev = await handle(app, notice(post=post.id))
        failed = ev.replies[-1]
        assert "No 🔁 Share URL button" in failed and "the userbot could not read it: Telegram refused the userbot" in failed
        assert reader.closed == 1 and ub.asked == 1  # given back at once, and not asked for again
        await app.db.close()

    run(go())


def test_the_real_userbot_reader_sees_a_keyboard_the_bot_cannot(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        app.tg.rights["invite"] = True
        cfg = SimpleNamespace(userbot_session="S", userbot_keep_admin=False, api_id=1, api_hash="x")
        ub = Userbot(app.tg, cfg, client_factory=lambda c: FakeUserClient(app.tg))
        app.ctx.userbot = ub
        post = file_post(app, hide=True)
        assert app.tg.msgs[post.id].reply_markup is None  # the bot sees no keyboard on it ...
        ev = await handle(app, notice(post=post.id))
        assert ev.replies[-1].startswith("✅ Post published successfully")  # ... the userbot does
        assert "read through the userbot" in ev.replies[-1] and button_url(last_post(app)[3]) == FILE_LINK
        assert not ub.lock.locked()  # the reader gave the userbot back
        await app.db.close()

    run(go())


# ------------------------------------------------------------------------------ what is refused, and when
def test_a_message_without_a_channel_fails_before_any_waiting(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app, buttons=False)
        ev = await handle(app, notice(channel=None, post=post.id))
        failed = ev.replies[-1]
        assert failed.startswith("❌ Publication failed:") and "no “Channel:” line" in failed
        assert app.clock.sleeps == [] and not app.tg.sent
        await app.db.close()

    run(go())


def test_channels_that_cannot_be_posted_in_are_refused(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)
        unknown = await handle(app, notice(channel=-1000000000777, post=post.id), mid=1)
        assert "is not connected to this bot" in unknown.replies[-1] and "/addchannel" in unknown.replies[-1]
        itself = await handle(app, notice(channel=DBM, post=post.id), mid=2)
        assert "Database Channel itself" in itself.replies[-1]
        words = await handle(app, notice(channel="Black Clover", post=post.id), mid=3)
        assert "“Black Clover” as the channel, which is not a channel id" in words.replies[-1]
        await app.db.set_channel_active(BLACK, False)
        off = await handle(app, notice(post=post.id), mid=4)
        assert "is not connected to this bot" in off.replies[-1]
        assert not app.tg.sent
        await app.db.close()

    run(go())


def test_the_episode_number_is_required_only_when_a_template_uses_it(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)
        ev = await handle(app, notice(episode=None, post=post.id), mid=1)
        assert "no “Episode:” line" in ev.replies[-1] and "/autopublish" in ev.replies[-1] and not app.tg.sent
        await save_templates(app.db, text="A new episode is out!", entities=[], button="Download")
        ev = await handle(app, notice(episode=None, post=post.id), mid=2)
        assert ev.replies[-1].startswith("✅")
        _peer, text, _ents, btn = last_post(app)
        assert text == "A new episode is out!" and btn.text == "Download"
        await app.db.close()

    run(go())


def test_without_a_post_number_the_latest_post_is_used_and_must_be_that_file(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        file_post(app, "Some Other Show - E01.mkv")
        file_post(app)  # the newest
        ev = await handle(app, notice(), mid=1)
        assert ev.replies[-1].startswith("✅") and last_post(app)[0].channel_id == BLACK
        ev = await handle(app, notice(file="Another File - E09.mkv"), mid=2)
        assert "The latest post of the Database Channel is the file “Black Clover - S2 E05.mkv”, not “Another File - E09.mkv”" in ev.replies[-1]
        assert len(app.tg.sent) == 1
        ev = await handle(app, notice(file=FILE.replace(".mkv", ".MKV")), mid=3)  # the extension and capitals do not matter
        assert ev.replies[-1].startswith("✅")
        await app.db.close()

    run(go())


def test_a_post_number_names_the_file_so_the_name_is_not_compared(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        file_post(app, "Newer File - E09.mkv")
        older = file_post(app)
        file_post(app, "Newest File - E10.mkv")
        ev = await handle(app, notice(post=older.id, file="whatever.mkv"))
        assert ev.replies[-1].startswith("✅") and "File: Black Clover - S2 E05.mkv" in ev.replies[-1]
        await app.db.close()

    run(go())


def test_a_post_that_is_not_a_file_is_refused(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        text_post = app.tg._new("hello", None, None)
        ev = await handle(app, notice(post=text_post.id))
        assert "is not a file (text)" in ev.replies[-1] and not app.tg.sent and app.clock.sleeps == []
        await app.db.close()

    run(go())


def test_a_share_url_button_without_a_usable_link_is_refused(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app, link="https://t.me/share/url?url=")
        ev = await handle(app, notice(post=post.id))
        assert "link I cannot use" in ev.replies[-1] and not app.tg.sent
        await app.db.close()

    run(go())


def test_when_telegram_refuses_the_post_the_reason_is_reported_and_nothing_is_kept(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)

        async def refuse(peer, message="", **kw):
            raise rpc("ChatWriteForbiddenError")

        app.tg.send_message = refuse
        ev = await handle(app, notice(post=post.id))
        failed = ev.replies[-1]
        assert failed.startswith("❌ Publication failed: Telegram would not let the bot post in “Black Clover English”")
        rows, total = await app.db.list_posts(BLACK, None, 0, 10)
        assert total == 0 and rows == []  # the draft made for it is gone
        await app.db.close()

    run(go())


def test_something_unexpected_still_gets_an_answer(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)

        async def boom(peer, message="", **kw):
            raise RuntimeError("kaput")

        app.tg.send_message = boom
        ev = await handle(app, notice(post=post.id))
        assert ev.replies[-1].startswith("❌ Publication failed: something went wrong inside the bot (RuntimeError: kaput)")
        await app.db.close()

    run(go())


# ---------------------------------------------------------------------------------------- the conversation
def test_the_same_message_is_answered_once_and_other_messages_not_at_all(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)
        first = await handle(app, notice(post=post.id), mid=7)
        again = await handle(app, notice(post=post.id), mid=7)  # delivered twice
        assert len(first.replies) == 2 and again.replies == [] and len(app.tg.sent) == 1
        chatter = await handle(app, "hello everybody", mid=8)
        assert chatter.replies == []
        await app.db.close()

    run(go())


def test_one_post_is_made_at_a_time(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        a, b = file_post(app, buttons=False), file_post(app, "Black Clover - S2 E06.mkv", buttons=False)
        pub = app.ctx.publisher
        now, most, orig = [0], [0], pub.publish

        async def counted(n):
            now[0] += 1
            most[0] = max(most[0], now[0])
            try:
                return await orig(n)
            finally:
                now[0] -= 1

        pub.publish = counted
        real = app.tg.get_messages

        async def later(peer, ids=None):
            if app.clock.t >= 3:
                for m in (a, b):
                    app.tg._set_markup(m, share_markup())
            return await real(peer, ids=ids)

        app.tg.get_messages = later
        one = Comm(notice(post=a.id, episode="05", ref="a"), mid=1)
        two = Comm(notice(post=b.id, episode="06", ref="b", file="Black Clover - S2 E06.mkv"), mid=2)
        await asyncio.gather(pub.handle(one), pub.handle(two))
        assert most[0] == 1 and len(app.tg.sent) == 2
        assert one.replies[-1].startswith("✅") and two.replies[-1].startswith("✅")
        assert [last[2].split("\n")[0] for last in app.tg.sent] == ["Episode 05", "Episode 06"]
        await app.db.close()

    run(go())


# ----------------------------------------------------------------------------------- the registered listener
def notice_handler(app):
    for builder, fn in app.tg.handlers:
        if isinstance(builder, events.NewMessage) and getattr(builder, "from_users", None):
            return fn
    return None


def test_the_listener_acts_only_on_the_autorename_bot_in_the_communication_chat(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        fn = notice_handler(app)
        assert fn is not None
        post = file_post(app)
        good = Comm(notice(post=post.id), mid=1)
        await fn(good)
        assert len(good.replies) == 2
        elsewhere = Comm(notice(post=post.id), mid=2, chat=-1000000000555)
        stranger = Comm(notice(post=post.id), mid=3, sender=12345)
        await fn(elsewhere)
        await fn(stranger)
        assert elsewhere.replies == [] and stranger.replies == [] and len(app.tg.sent) == 1
        await app.db.close()

    run(go())


def test_the_listener_is_there_only_when_both_chats_are_known(tmp_path):
    async def go():
        for n, cfg in enumerate(({}, {"db_channel": DBM}, {"comm_chat": COMM}, {"db_channel": DBM, "comm_chat": COMM})):
            (tmp_path / f"a{n}").mkdir(exist_ok=True)
            app = App(tmp_path / f"a{n}", **cfg)
            await app.start()
            on = bool(cfg.get("db_channel") and cfg.get("comm_chat"))
            assert (app.ctx.publisher is not None) is on and (notice_handler(app) is not None) is on
            await app.text("/autopublish")  # the command is there either way
            assert "Automatic publishing" in norm(app.out.last_text)
            await app.db.close()

    run(go())


# ------------------------------------------------------------------------------------------ /autopublish
def test_the_command_shows_the_defaults_and_what_it_does(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopublish")
        shown = norm(app.out.last_text)
        assert "Automatic publishing" in shown and "On." in shown and str(COMM) in shown and str(DBM) in shown
        assert "Episode {episode}" in shown and "Click the button below to download 👇" in shown
        assert "Download Episode {episode}" in shown and "{channel}" in shown and "(default)" in shown
        assert app.out.last_buttons() == [["✏️ Message text", "✏️ Button name"], ["👁 Preview"], ["✖️ Close"]]
        await app.db.close()

    run(go())


def test_the_command_says_when_it_is_off(tmp_path):
    async def go():
        app = App(tmp_path, db_channel=DBM)
        await app.start()
        await app.text("/autopublish")
        assert "Off." in norm(app.out.last_text) and "COMMUNICATION_CHAT" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_the_message_text_is_changed_from_the_command(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopublish")
        await app.press("dpb:t")
        assert "Send the new message text" in norm(app.out.last_text) and "Default:" in norm(app.out.last_text)
        await app.text("Episode {episode} is out!\n\nGet it below 👇")
        shown = norm(app.out.last_text)
        assert shown.startswith("✅ Message text saved.") and "Episode {episode} is out!" in shown and "(your own)" in shown
        assert ["↩️ Default text"] in app.out.last_buttons()
        t = await load_templates(app.db)
        assert t.text == "Episode {episode} is out!\n\nGet it below 👇" and t.custom_text and not t.custom_button
        await app.db.close()

    run(go())


def test_html_or_markdown_typed_as_the_text_becomes_formatting(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.press("dpb:t")
        await app.text("<b>Episode {episode}</b>\nBelow 👇")
        assert "Formatting read from HTML" in norm(app.out.last_text)
        t = await load_templates(app.db)
        assert t.text == "Episode {episode}\nBelow 👇" and t.entities and "has formatting" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_the_button_name_is_changed_from_the_command(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopublish")
        await app.press("dpb:b")
        assert "Send the new button name" in norm(app.out.last_text)
        await app.text("Get Episode {episode number}")
        assert norm(app.out.last_text).startswith("✅ Button name saved.")
        t = await load_templates(app.db)
        assert t.button == "Get Episode {episode number}" and t.custom_button and not t.custom_text
        assert ["↩️ Default button"] in app.out.last_buttons()
        await app.db.close()

    run(go())


@pytest.mark.parametrize(
    "mode,text,complaint",
    [
        ("dpb:t", "Hello {ep}", "{ep} is not a variable"),
        ("dpb:t", "x" * 3001, "too long"),
        ("dpb:t", "   ", "Send some text"),
        ("dpb:b", "Get {nr}", "{nr} is not a variable"),
        ("dpb:b", "y" * 61, "too long"),
        ("dpb:b", "one\ntwo", "one line"),
    ],
)
def test_input_that_cannot_work_is_refused_and_the_old_one_stays(tmp_path, mode, text, complaint):
    async def go():
        app = await make_app(tmp_path)
        await app.press(mode)
        await app.text(text)
        assert complaint in norm(app.out.last_text)
        assert (await load_templates(app.db)).custom_text is False and (await load_templates(app.db)).custom_button is False
        assert app.ctx.state.get(app.uid)  # still waiting for a better one (/cancel stops it)
        await app.db.close()

    run(go())


def test_the_defaults_come_back_with_the_reset_buttons(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await save_templates(app.db, text="Mine {episode}", entities=[], button="Mine {episode}")
        await app.text("/autopublish")
        assert ["↩️ Default text", "↩️ Default button"] in app.out.last_buttons()
        await app.press("dpb:rt")
        assert "default again" in norm(app.out.last_text) and ["↩️ Default button"] in app.out.last_buttons()
        await app.press("dpb:rb")
        t = await load_templates(app.db)
        assert (t.text, t.button) == (DEFAULT_TEXT, DEFAULT_BUTTON)
        assert app.out.last_buttons() == [["✏️ Message text", "✏️ Button name"], ["👁 Preview"], ["✖️ Close"]]
        await app.db.close()

    run(go())


def test_the_preview_shows_a_post_for_episode_05(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await save_templates(app.db, button="Get {episode} from {channel}")
        await app.press("dpb:v")
        _peer, text, _ents, btn = last_post(app)
        assert text == DEFAULT_POST_TEXT and btn.text == "Get 05 from Your channel" and button_url(btn) == "https://t.me/telegram"
        assert "A post for episode 05" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_close_leaves_nothing_waiting(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.press("dpb:t")
        assert app.ctx.state.get(app.uid)
        await app.press("dpb:x")
        assert norm(app.out.last_text) == "Closed." and not app.ctx.state.get(app.uid)
        await app.db.close()

    run(go())


def test_only_the_owner_changes_it(tmp_path):
    async def go():
        app = await make_app(tmp_path, admins=frozenset({2}))
        app.uid = 2  # an admin, not the owner
        await app.text("/autopublish")
        assert "Automatic publishing" not in norm(app.out.last_text or "")
        await app.press("dpb:t")
        assert "Only the bot owner" in norm(app.out.last_text) and not app.ctx.state.get(2)
        await app.press("dpb:rt")
        assert "Only the bot owner" in norm(app.out.last_text)
        await app.db.close()

    run(go())
