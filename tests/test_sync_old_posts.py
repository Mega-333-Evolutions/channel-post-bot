"""The sync and the posts it reads into My posts from before the bot was added.

* a saved post that was deleted in the channel leaves My posts - also at the first look, when there is no earlier look to
  compare with: the bot only holds back when it can't read ANY message of the channel;
* what Telegram itself does to a channel (a copyright strike) is never taken for an edit or a deletion;
* the older posts are followed like every other saved post: what admins change in them outside the bot (edited, deleted)
  reaches My posts, in the timed check and in the live watch.
"""
from telethon import types

from app.sync_engine import sees_live_messages, sync_channel
from app.tgutil import media_info

from .fakes import FakeChannelClient, markup, photo_media, url_btn
from .test_restricted import CHANNEL_NOTICE, NOTICE, seven, strike
from .test_sync import old_msg, opts, run, world
from .test_syncer import delete_update, edit_update, make_app

PEER = types.InputPeerChannel(1, 5)


# ================================================================================ deleted posts at the first look
def test_saved_posts_deleted_in_the_channel_leave_my_posts_at_the_first_look(tmp_path):
    """The real case: seven saved posts, all of them deleted in the channel while the bot was not looking, and older posts
    that the very same look reads in. The bot reads those, so it is not blind: the seven are really gone."""
    async def go():
        chan, db, ch = await world(tmp_path, seven())  # seven saved posts ...
        chan.msgs.clear()  # ... all deleted in the channel
        for i in range(8, 18):
            chan.msgs[i] = old_msg(i, f"older post {i}")
        chan._top = chan._pts = 17
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.first_look and rep.anomaly is None and rep.error is None
        assert sorted(rep.deleted) == list(range(1, 8)) and rep.checked == 7
        assert rep.imported == 10 and rep.history_done
        assert [p.message_id for p in await db.sent_posts(1)] == list(range(8, 18))  # the seven are gone, the older ones are in
        await db.close()

    run(go())


def test_the_same_without_reading_the_older_posts(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        chan.msgs.clear()
        chan.msgs[8] = old_msg(8, "a post nobody saved")
        chan._top = chan._pts = 8
        rep = await sync_channel(chan, db, ch, opts(history=False))
        assert sorted(rep.deleted) == list(range(1, 8)) and rep.anomaly is None and rep.imported == 0
        assert await db.sent_posts(1) == []
        await db.close()

    run(go())


def test_the_bot_looks_downwards_from_the_event_counter_for_a_message_it_can_read(tmp_path):
    async def go():
        chan = FakeChannelClient({1: old_msg(1, "a"), 2: old_msg(2, "b")}, username="chan")
        chan._pts = 1000  # a thousand events since: the messages are far below the counter
        assert await sees_live_messages(chan, PEER, 1000, pause=0)
        assert not await sees_live_messages(chan, PEER, 1000, pause=0, tries=3)  # 300 ids are not enough to get down to them
        assert not await sees_live_messages(chan, PEER, None) and not await sees_live_messages(chan, PEER, 0)
        empty = FakeChannelClient({}, username="chan")
        assert not await sees_live_messages(empty, PEER, 50, pause=0)
        # a service message is something the bot can read, too
        only_notice = FakeChannelClient({3: types.MessageService(id=3, peer_id=types.PeerChannel(1), date=None, action=types.MessageActionPinMessage())}, username="chan")
        assert await sees_live_messages(only_notice, PEER, 3, pause=0)

    run(go())


def test_when_the_counter_is_far_above_the_messages_the_deleted_posts_still_leave(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        chan.msgs.clear()
        chan.msgs[8] = old_msg(8, "still there")
        chan._top, chan._pts = 8, 1000  # many edits and deletions since
        rep = await sync_channel(chan, db, ch, opts(history=False))
        assert sorted(rep.deleted) == list(range(1, 8)) and rep.anomaly is None
        await db.close()

    run(go())


def test_when_nothing_at_all_can_be_read_nothing_is_removed_and_the_report_says_why(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        chan.msgs.clear()  # the bot sees nothing: it may have lost access
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.deleted == [] and len(await db.sent_posts(1)) == 7
        assert rep.anomaly and "All 7 saved posts look deleted" in rep.anomaly
        assert "can't read any other message of the channel" in rep.anomaly and "none was removed" in rep.anomaly
        await db.close()

    run(go())


def test_a_long_job_that_starts_while_the_bot_is_unsure_cuts_the_look_short(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        chan.msgs.clear()
        calls = [0]

        def busy():
            calls[0] += 1
            return calls[0] > 1  # free while the posts are read, busy by the time the bot has to decide

        rep = await sync_channel(chan, db, ch, opts(), busy=busy)
        assert rep.busy and rep.deleted == [] and rep.anomaly is None and len(await db.sent_posts(1)) == 7
        await db.close()

    run(go())


# ============================================================================================ what Telegram does
def test_a_struck_channel_is_left_alone_even_when_every_saved_post_would_look_deleted(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        chan.restriction = CHANNEL_NOTICE  # the whole channel is struck
        chan.msgs.clear()  # ... and Telegram shows nothing of it
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.restricted_channel == CHANNEL_NOTICE
        assert rep.deleted == [] and rep.edited == [] and rep.adopted == [] and rep.imported == 0 and not rep.checked
        posts = await db.sent_posts(1)
        assert len(posts) == 7 and [p.text for p in posts] == [f"post number {i}" for i in range(1, 8)]
        await db.close()

    run(go())


def test_posts_held_back_by_telegram_are_neither_edited_nor_removed_even_when_every_post_is_held_back(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        before = {p.message_id: (p.text, p.buttons, p.entities) for p in await db.sent_posts(1)}
        for m in chan.msgs.values():  # Telegram's own mark and its notice instead of the text and the buttons
            strike(m)
            m.message, m.reply_markup = NOTICE, None
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.restricted) == list(range(1, 8))
        assert rep.edited == [] and rep.deleted == [] and rep.anomaly is None and rep.buttons_missing == []
        assert {p.message_id: (p.text, p.buttons, p.entities) for p in await db.sent_posts(1)} == before
        await db.close()

    run(go())


def test_a_notice_without_any_mark_is_not_taken_for_an_edit_either(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        for m in chan.msgs.values():
            m.message = NOTICE  # some places hand out the notice as the text
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.restricted) == list(range(1, 8)) and rep.edited == [] and rep.deleted == []
        assert [p.text for p in await db.sent_posts(1)] == [f"post number {i}" for i in range(1, 8)]
        await db.close()

    run(go())


def test_held_back_posts_stay_while_the_posts_that_are_really_gone_leave(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        for i in (1, 2, 3):
            strike(chan.msgs[i])
            chan.msgs[i].message = NOTICE
        for i in (4, 5, 6, 7):
            del chan.msgs[i]
        chan._pts += 4
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.deleted) == [4, 5, 6, 7] and sorted(rep.restricted) == [1, 2, 3] and rep.edited == []
        posts = await db.sent_posts(1)
        assert [p.message_id for p in posts] == [1, 2, 3] and [p.text for p in posts] == [f"post number {i}" for i in (1, 2, 3)]
        await db.close()

    run(go())


# ================================================================== the older posts are followed like any other post
def older(n=8):
    return {
        i: old_msg(
            i, f"older post {i}", markup=markup([url_btn("Watch", f"https://t.me/old/{i}")]),
            media=photo_media(10 + i) if i == 4 else None,
        )
        for i in range(1, n + 1)
    }


async def imported(tmp_path, n=8):
    """A channel whose posts all come from before the bot was added: the first look reads them into My posts."""
    chan, db, ch = await world(tmp_path, older(n), save=False)
    rep = await sync_channel(chan, db, ch, opts())
    assert rep.first_look and rep.imported == n and rep.history_done
    assert {p.source for p in await db.sent_posts(1)} == {"adopted"}
    return chan, db, ch


def test_what_admins_do_to_the_older_posts_outside_the_bot_reaches_my_posts(tmp_path):
    async def go():
        chan, db, ch = await imported(tmp_path)
        chan.msgs[2].message = "older post 2, corrected by an admin"
        chan.msgs[2].entities = [types.MessageEntityBold(0, 5)]
        chan.msgs[3].reply_markup = markup([url_btn("New link", "https://t.me/new/3"), url_btn("Site", "https://example.org")])
        chan.msgs[4].media = photo_media(99)  # the picture was replaced
        del chan.msgs[5]
        chan._pts += 4
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.edited) == [2, 3, 4] and rep.deleted == [5] and rep.checked == 8 and rep.anomaly is None
        rows = {p.message_id: p for p in await db.sent_posts(1)}
        assert sorted(rows) == [1, 2, 3, 4, 6, 7, 8]
        assert rows[2].text == "older post 2, corrected by an admin" and rows[2].entities == [{"k": "bold", "o": 0, "l": 5}]
        assert rows[3].buttons == [[{"t": "New link", "u": "https://t.me/new/3"}, {"t": "Site", "u": "https://example.org"}]]
        assert rows[4].media_file_id == media_info(chan.msgs[4])[1]
        assert {p.source for p in rows.values()} == {"adopted"}  # still the posts of others, now up to date
        again = await sync_channel(chan, db, ch, opts())
        assert again.changed is False and again.edited == [] and again.deleted == []
        await db.close()

    run(go())


def test_every_older_post_is_compared_on_each_look_not_only_the_newest(tmp_path):
    async def go():
        chan, db, ch = await imported(tmp_path, n=250)  # three requests' worth of posts
        chan.msgs[1].message = "the oldest post, edited"
        chan.msgs[250].message = "the newest post, edited"
        del chan.msgs[120]
        chan._pts += 3
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.checked == 250 and sorted(rep.edited) == [1, 250] and rep.deleted == [120]
        assert (await db.find_by_message(1, 1)).text == "the oldest post, edited"
        assert await db.find_by_message(1, 120) is None
        await db.close()

    run(go())


def test_the_live_watch_follows_an_admin_who_edits_or_deletes_an_older_post(tmp_path):
    async def go():
        app = await make_app(tmp_path)  # nothing saved by the bot
        sy = app.ctx.syncer
        for i, m in older(6).items():
            app.tg.msgs[i] = m
        app.tg._top = app.tg._pts = 6
        await app.text("/sync")  # the first look reads the older posts into My posts
        assert len(await app.db.sent_posts(1)) == 6
        # an admin edits one of them and deletes another, from the Telegram app
        m = app.tg.msgs[2]
        m.message, m.entities = "older post 2, with a correction", [types.MessageEntityItalic(0, 5)]
        await app.raw(edit_update(m))
        del app.tg.msgs[5]
        await app.raw(delete_update(1, [5]))
        await sy.flush_events()
        p = await app.db.find_by_message(1, 2)
        assert p.text == "older post 2, with a correction" and p.entities == [{"k": "italic", "o": 0, "l": 5}] and p.source == "adopted"
        assert sy.last_reports[1].edited == [2]
        assert await app.db.find_by_message(1, 5) is None
        assert sorted(x.message_id for x in await app.db.sent_posts(1)) == [1, 2, 3, 4, 6]
        await app.db.close()

    run(go())


def test_the_timed_check_follows_the_older_posts_too(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        sy = app.ctx.syncer
        plain = sy.options
        sy.options = lambda **kw: plain(grace=0, **kw)  # in real life the next check is minutes later: nothing is too fresh
        for i, m in older(6).items():
            app.tg.msgs[i] = m
        app.tg._top = app.tg._pts = 6
        await sy.run_all()  # the timed check: the first look reads the older posts
        assert len(await app.db.sent_posts(1)) == 6
        app.tg.msgs[3].message = "older post 3, edited while the bot was off"
        del app.tg.msgs[1]
        app.tg._pts += 2
        await sy.run_all()  # ... and the next one compares them
        assert (await app.db.find_by_message(1, 3)).text == "older post 3, edited while the bot was off"
        assert await app.db.find_by_message(1, 1) is None
        await app.db.close()

    run(go())
