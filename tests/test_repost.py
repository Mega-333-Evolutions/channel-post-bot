"""The repost engine against a simulated channel: what is copied how, buttons verified, deletes with a userbot."""
import asyncio

from telethon import functions, types

from app.repost_engine import RepostOptions, delete_copies, plan_repost, run_repost
from app.tgutil import button_url, is_callback_button, utf16_len

from .dbutil import db_url_for, fresh_db
from .fakes import (
    FakeChannelClient,
    cb_btn,
    make_msg,
    markup,
    photo_media,
    quote,
    rpc,
    service_msg,
    url_btn,
    video_media,
)

OLD, NEW = "oldbot", "goku"
Q_TEXT = "Click the button below 👇"
CAP = "Caption 😀 with link"
TYPED = "see https://t.me/oldbot?start=Q done"


def build():
    """A little channel: pure posts, button posts, hyperlinks, an album, a typed link, a service message."""
    typed_bold = types.MessageEntityBold(TYPED.index("done"), 4)
    return {
        1: make_msg(1, "Welcome", [types.MessageEntityBold(0, 7)]),
        2: make_msg(
            2,
            Q_TEXT,
            [quote(0, utf16_len(Q_TEXT), True)],
            markup(
                [url_btn("Download Episode 1", f"https://t.me/{OLD}?start=AAA")],
                [url_btn("Other", "https://example.com/x"), cb_btn("👍 3", b"r1")],
            ),
        ),
        3: make_msg(
            3, CAP, [types.MessageEntityBold(0, 7), types.MessageEntityTextUrl(16, 4, "https://t.me/OldBot?start=ZZZ")],
            media=photo_media(31),
        ),
        4: make_msg(4, "", media=photo_media(41), grouped_id=50),
        5: make_msg(5, "album caption", [types.MessageEntityItalic(0, 5)], media=photo_media(42), grouped_id=50),
        6: service_msg(6),
        8: make_msg(8, "Movie", markup=markup([url_btn("Get", f"https://t.me/{OLD}?start=VVV")]), media=video_media(81)),
        9: make_msg(9, TYPED, [typed_bold]),
        10: make_msg(10, "Bye", [types.MessageEntityItalic(0, 3)]),
    }


async def setup(tmp_path, chan=None, **mig_kw):
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Test", "testch", 99)
    ch = await db.get_channel(1)
    chan = chan or FakeChannelClient(build())
    kw = dict(old=OLD, new=NEW, include_typed=True, include_posts=False, first_id=1, last_id=max(chan.msgs), partial=False, user_id=99)
    kw.update(mig_kw)
    await db.create_migration("m1", 1, **kw)
    return db, ch, chan, await db.get_migration("m1")


def edit_requests(chan):
    return [r for r in chan.requests if isinstance(r, functions.messages.EditMessageRequest)]


def forwarded_ids(chan):
    return [list(r.id) for r in chan.requests if isinstance(r, functions.messages.ForwardMessagesRequest)]


def test_plan_counts(tmp_path):
    async def run():
        db, ch, chan, _ = await setup(tmp_path)
        plan = await plan_repost(chan, ch, RepostOptions(OLD, NEW))
        assert (plan.first, plan.last, plan.error) == (1, 10, None)
        assert (plan.units, plan.messages, plan.albums, plan.service) == (7, 8, 1, 1)
        assert (plan.text, plan.media) == (4, 4)
        assert plan.link_posts == 4 and (plan.n_buttons, plan.n_links, plan.n_typed) == (2, 1, 1)
        assert plan.dropped_posts == 1  # post 2 carries somebody else's 👍 button
        await db.close()

    asyncio.run(run())


def test_pure_posts_are_forwarded_and_button_posts_are_created_with_their_buttons(tmp_path):
    async def run():
        db, ch, chan, mig = await setup(tmp_path)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.copied_messages, res.failed, res.aborted) == (7, 8, [], None)
        assert (res.forwarded, res.rebuilt, res.repaired) == (3, 4, 0)
        # only posts that need no change are copied by Telegram ...
        assert forwarded_ids(chan) == [[1], [4, 5], [10]]
        # ... and nothing was ever edited afterwards: buttons are part of the request that creates the post
        assert edit_requests(chan) == []

        new = {i: m for i, m in chan.msgs.items() if i > 10}
        assert list(new) == list(range(11, 19))  # same order as the originals
        # 11 = Welcome (forwarded, untouched)
        assert new[11].message == "Welcome" and isinstance(new[11].entities[0], types.MessageEntityBold)
        # 12 = the button post: final text, expandable quote kept, link button swapped, foreign button dropped
        m = new[12]
        assert m.message == Q_TEXT
        assert isinstance(m.entities[0], types.MessageEntityBlockquote) and m.entities[0].collapsed
        rows = m.reply_markup.rows
        assert len(rows) == 2 and len(rows[0].buttons) == 1 and len(rows[1].buttons) == 1
        assert button_url(rows[0].buttons[0]) == f"https://t.me/{NEW}?start=AAA"
        assert rows[0].buttons[0].text == "Download Episode 1"
        assert button_url(rows[1].buttons[0]) == "https://example.com/x"
        assert not any(is_callback_button(b) for r in rows for b in r.buttons)
        # 13 = photo with a hyperlink to the old name: media re-sent by reference, link swapped, bold kept
        m = new[13]
        assert m.message == CAP and m.media.photo.id == 31
        ents = {type(e).__name__: e for e in m.entities}
        assert ents["MessageEntityTextUrl"].url == f"https://t.me/{NEW}?start=ZZZ" and ents["MessageEntityTextUrl"].offset == 16
        assert ents["MessageEntityBold"].length == 7
        # 14, 15 = the album, forwarded as one unit with a new shared group id
        assert new[14].grouped_id and new[14].grouped_id == new[15].grouped_id != 50
        assert new[15].message == "album caption"
        # 16 = video with a button; 17 = typed link swapped and the bold span moved with the text
        assert new[16].media.document.id == 81 and button_url(new[16].reply_markup.rows[0].buttons[0]) == f"https://t.me/{NEW}?start=VVV"
        assert new[17].message == TYPED.replace(OLD, NEW)
        b = new[17].entities[0]
        assert new[17].message.encode("utf-16-le")[b.offset * 2 : (b.offset + b.length) * 2].decode("utf-16-le") == "done"
        assert new[18].message == "Bye"

        # everything is registered in My posts exactly as it looks in the channel
        done = await db.migration_done_old_ids("m1")
        assert done == {1, 2, 3, 4, 5, 8, 9, 10}
        p12 = await db.find_by_message(1, 12)
        assert p12.source == "bot" and p12.status == "sent" and p12.text == Q_TEXT
        assert p12.buttons == [
            [{"t": "Download Episode 1", "u": f"https://t.me/{NEW}?start=AAA"}],
            [{"t": "Other", "u": "https://example.com/x"}],
        ]
        p13 = await db.find_by_message(1, 13)
        assert p13.media_kind == "photo" and p13.media_file_id.startswith("tl:")
        assert (await db.migration_counts("m1"))["copied"] == 8
        # the originals are untouched
        assert all(i in chan.msgs for i in (1, 2, 3, 4, 5, 8, 9, 10))
        await db.close()

    asyncio.run(run())


def test_channel_that_forbids_forwarding_gets_everything_rebuilt(tmp_path):
    async def run():
        chan = FakeChannelClient(build())
        chan.restrict_forwards = True
        db, ch, chan, mig = await setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.forwarded, res.rebuilt) == (7, [], 0, 7)
        assert len(forwarded_ids(chan)) == 1  # tried once, then remembered that it is not allowed
        new = {i: m for i, m in chan.msgs.items() if i > 10}
        assert [new[i].message for i in sorted(new)] == [
            "Welcome", Q_TEXT, CAP, "", "album caption", "Movie", TYPED.replace(OLD, NEW), "Bye",
        ]
        a, b = new[14], new[15]
        assert a.grouped_id == b.grouped_id and a.media.photo.id == 41 and b.media.photo.id == 42
        assert isinstance(b.entities[0], types.MessageEntityItalic)
        await db.close()

    asyncio.run(run())


def test_a_keyboard_that_is_not_shown_is_noticed_and_repaired(tmp_path):
    async def run():
        chan = FakeChannelClient(build())
        chan.hide_keyboard_on_create = 1  # Telegram "forgets" the keyboard of the next button post
        db, ch, chan, mig = await setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert res.failed == [] and res.copied_units == 7 and res.repaired == 1
        live = chan.msgs[12].reply_markup
        assert live is not None and button_url(live.rows[0].buttons[0]) == f"https://t.me/{NEW}?start=AAA"
        # Telegram said "not modified" to the plain edit, so the two-step edit was needed (a stand-in, then the real one)
        assert len(chan.edits) == 2
        assert button_url(chan.edits[0].reply_markup.rows[0].buttons[0]) == "https://t.me/telegram"
        assert chan.edits[1].reply_markup is not None
        await db.close()

    asyncio.run(run())


def test_a_copy_that_never_shows_its_buttons_is_removed_and_the_original_kept(tmp_path):
    async def run():
        chan = FakeChannelClient(build())
        chan.drop_all_markup = True
        db, ch, chan, mig = await setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert res.failed == [(2, "ButtonsNotShown"), (8, "ButtonsNotShown")] and res.aborted is None
        assert res.copied_units == 5
        # no copy of the failed posts is left, and they are not registered
        assert sorted(i for i in chan.msgs if i > 10 and chan.msgs[i].message in (Q_TEXT, "Movie")) == []
        done = await db.migration_done_old_ids("m1")
        assert 2 not in done and 8 not in done and {1, 3, 4, 5, 9, 10} <= done
        assert 2 in chan.msgs and 8 in chan.msgs and chan.msgs[2].reply_markup is not None
        await db.close()

    asyncio.run(run())


def test_stop_then_continue_copies_nothing_twice(tmp_path):
    async def run():
        db, ch, chan, mig = await setup(tmp_path)
        seen = []

        async def progress(r):
            seen.append(r.copied_units)

        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0, progress=progress, should_stop=lambda: len(seen) >= 3)
        assert res.stopped and res.copied_units == 3
        res2 = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert not res2.stopped and res2.skipped_done == 3 and res2.copied_units == 4
        texts = [m.message for i, m in sorted(chan.msgs.items()) if i > 10]
        assert texts.count("Welcome") == 1 and texts.count("Bye") == 1 and len(texts) == 8
        await db.close()

    asyncio.run(run())


def test_a_fatal_telegram_error_stops_the_run(tmp_path):
    async def run():
        chan = FakeChannelClient(build())
        chan.fail["SendMessageRequest"] = rpc("ChatWriteForbiddenError")
        db, ch, chan, mig = await setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert res.aborted == "ChatWriteForbiddenError" and res.copied_units == 1  # only the forwarded first post
        await db.close()

    asyncio.run(run())


# ------------------------------------------------------------------------------------------ deleting
async def copied(tmp_path, chan=None):
    db, ch, chan, mig = await setup(tmp_path, chan)
    await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
    return db, ch, chan, await db.get_migration("m1")


def test_delete_old_posts_with_the_bot_alone(tmp_path):
    async def run():
        db, ch, chan, mig = await copied(tmp_path)
        res = await delete_copies(chan, db, ch, mig, "old", delay=0)
        assert (res.deleted, res.remaining, res.error, res.by_userbot, res.blocked) == (8, 0, None, 0, False)
        assert sorted(chan.msgs) == [6] + list(range(11, 19))
        assert await db.find_by_message(1, 2) is None  # nothing left of the old posts in the database
        assert await db.find_by_message(1, 12) is not None  # the copies stay
        await db.close()

    asyncio.run(run())


def test_old_posts_the_bot_cannot_delete_go_to_the_userbot(tmp_path):
    async def run():
        db, ch, chan, mig = await copied(tmp_path)
        chan.undeletable = {1, 2, 3, 4, 5}  # "older than 48 hours" for the bot
        calls = []

        async def userbot(ids):
            calls.append(list(ids))
            for i in ids:
                chan.msgs.pop(i, None)

        res = await delete_copies(chan, db, ch, mig, "old", delay=0, fallback=userbot)
        assert calls == [[1, 2, 3, 4, 5]]
        assert (res.deleted, res.by_userbot, res.remaining, res.error, res.tried_userbot) == (8, 5, 0, None, True)
        assert sorted(chan.msgs) == [6] + list(range(11, 19))
        await db.close()

    asyncio.run(run())


def test_a_failing_userbot_is_reported_and_what_the_bot_could_delete_still_counts(tmp_path):
    async def run():
        db, ch, chan, mig = await copied(tmp_path)
        chan.undeletable = {1, 2, 3, 4, 5}
        calls = []

        async def userbot(ids):
            calls.append(ids)
            raise RuntimeError("the userbot is banned here")

        res = await delete_copies(chan, db, ch, mig, "old", delay=0, fallback=userbot)
        assert res.deleted == 3 and res.remaining == 5 and res.by_userbot == 0
        assert res.fallback_error == "the userbot is banned here" and res.blocked and res.error is None
        assert len(calls) == 1
        await db.close()

    asyncio.run(run())


def many_messages(n):
    return {i: make_msg(i, f"post {i}") for i in range(1, n + 1)}


def test_an_undeletable_batch_does_not_stop_the_newer_ones(tmp_path):
    async def run():
        chan = FakeChannelClient(many_messages(250))
        db, ch, chan, mig = await setup(tmp_path, chan, old=None, new=None)
        rows = [{"old_id": i, "new_id": 1000 + i, "post": {"text": "x", "entities": [], "buttons": [], "media_kind": None, "media_file_id": None, "link_preview": False}} for i in range(1, 251)]
        await db.record_repost("m1", 1, rows, 99)
        chan.undeletable = set(range(1, 101))
        res = await delete_copies(chan, db, ch, mig, "old", delay=0)
        assert len(chan.delete_requests) == 3  # all three batches were tried
        assert (res.deleted, res.remaining, res.blocked, res.error) == (150, 100, True, None)
        await db.close()

    asyncio.run(run())


def test_missing_delete_right_stops_after_the_first_batch(tmp_path):
    async def run():
        chan = FakeChannelClient(many_messages(250))
        db, ch, chan, mig = await setup(tmp_path, chan, old=None, new=None)
        rows = [{"old_id": i, "new_id": 1000 + i, "post": {"text": "x", "entities": [], "buttons": [], "media_kind": None, "media_file_id": None, "link_preview": False}} for i in range(1, 251)]
        await db.record_repost("m1", 1, rows, 99)
        chan.delete_error = rpc("ChatAdminRequiredError")
        res = await delete_copies(chan, db, ch, mig, "old", delay=0)
        assert res.error == "ChatAdminRequiredError" and res.deleted == 0 and len(chan.delete_requests) == 1

        # with a userbot that can do it, the bot's missing right is no problem
        async def userbot(ids):
            for i in ids:
                chan.msgs.pop(i, None)

        res = await delete_copies(chan, db, ch, mig, "old", delay=0, fallback=userbot)
        assert (res.deleted, res.by_userbot, res.remaining, res.error) == (250, 250, 0, None)
        await db.close()

    asyncio.run(run())


def test_undo_removes_only_the_copies(tmp_path):
    async def run():
        db, ch, chan, mig = await copied(tmp_path)
        res = await delete_copies(chan, db, ch, mig, "new", delay=0)
        assert res.deleted == 8 and res.remaining == 0
        assert sorted(chan.msgs) == [1, 2, 3, 4, 5, 6, 8, 9, 10]
        await db.close()

    asyncio.run(run())
