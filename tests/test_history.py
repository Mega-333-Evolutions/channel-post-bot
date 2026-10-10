"""The posts that were in a channel before the bot was added are read into My posts - and what the sync does with a post
that was deleted in the channel."""
import datetime
import itertools
from types import SimpleNamespace

from sqlalchemy import event
from sqlalchemy.orm import Session as SyncSession
from telethon import types

from app import sync_engine
from app.db import Post, as_utc, utcnow
from app.sync_engine import live_fields, sync_channel

from .fakes import markup, photo_media, service_msg, url_btn, video_media
from .harness import norm
from .test_restricted import NOTICE, strike
from .test_sync import OLD, foreign, old_msg, opts, run, saved, world
from .test_syncer import delete_update, make_app, three as three_posts


def history(n, first=1):
    return {i: old_msg(i, f"older post {i}") for i in range(first, first + n)}


def ticking(monkeypatch, step=1.0):
    """The sync engine's clock: every look at it is `step` seconds later than the one before."""
    count = itertools.count(1)
    monkeypatch.setattr(sync_engine, "time", SimpleNamespace(monotonic=lambda: next(count) * step))


# ===================================================================================== what comes into My posts
def test_the_older_posts_come_into_my_posts_with_everything_they_have(tmp_path):
    async def go():
        msgs = {
            1: old_msg(1, "plain"),
            2: old_msg(2, "bold", entities=[types.MessageEntityBold(0, 4)]),
            3: old_msg(3, "with buttons", markup=markup([url_btn("Watch", "https://t.me/a/1")])),
            4: old_msg(4, "a photo", media=photo_media(11)),
            5: old_msg(5, "", media=video_media(21)),
        }
        chan, db, ch = await world(tmp_path, msgs, save=False)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.first_look and rep.imported == 5 and rep.history_done and rep.adopted == []
        by = {p.message_id: p for p in await db.sent_posts(1)}
        assert sorted(by) == [1, 2, 3, 4, 5]
        assert by[2].entities == [{"k": "bold", "o": 0, "l": 4}]
        assert by[3].buttons == [[{"t": "Watch", "u": "https://t.me/a/1"}]]
        assert (by[4].media_kind, by[4].media_file_id) == (live_fields(msgs[4])["media_kind"], live_fields(msgs[4])["media_file_id"])
        assert by[4].media_kind and by[5].media_kind and by[5].text == ""
        assert all(p.source == "adopted" and p.created_by == 0 and p.status == "sent" for p in by.values())
        assert as_utc(by[1].sent_at) == OLD  # when the post was sent, not when the bot found it
        await db.close()

    run(go())


def test_service_messages_polls_and_stickers_among_the_older_posts_are_left_alone(tmp_path):
    async def go():
        msgs = {1: old_msg(1, "a"), 2: service_msg(2), 3: old_msg(3, ""), 4: old_msg(4, "b")}  # 3: nothing My posts can hold
        chan, db, ch = await world(tmp_path, msgs, save=False)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.imported == 2 and rep.history_unsupported == 1
        assert [p.message_id for p in await db.sent_posts(1)] == [1, 4]
        assert 2 in chan.msgs and 3 in chan.msgs  # nothing is deleted from the channel
        await db.close()

    run(go())


def test_posts_the_bot_already_has_are_not_taken_again(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {1: old_msg(1, "a"), 2: old_msg(2, "b"), 3: old_msg(3, "c")}, save=False)
        await saved(db, chan.msgs[2])  # made through the bot
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.imported == 2
        assert (await db.find_by_message(1, 2)).source == "bot"
        assert len(await db.sent_posts(1)) == 3
        await db.close()

    run(go())


def test_a_restricted_old_post_and_the_notice_text_are_not_imported(tmp_path):
    async def go():
        msgs = {1: old_msg(1, "fine"), 2: strike(old_msg(2, "hidden")), 3: old_msg(3, NOTICE)}
        chan, db, ch = await world(tmp_path, msgs, save=False)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.imported == 1 and sorted(rep.restricted) == [2, 3]
        assert [p.message_id for p in await db.sent_posts(1)] == [1]
        await db.close()

    run(go())


def test_a_restricted_channel_gets_no_older_posts_and_no_record(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, history(5), save=False)
        chan.restriction = NOTICE
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.restricted_channel == NOTICE and rep.imported == 0
        assert await db.sent_posts(1) == [] and await db.get_history(1) is None and await db.get_sync(1) is None
        chan.restriction = None  # the strike is lifted: now the posts come in
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.imported == 5
        await db.close()

    run(go())


def test_switching_the_import_off_keeps_to_what_happens_from_now_on(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, history(5), save=False)
        rep = await sync_channel(chan, db, ch, opts(history=False))
        assert rep.first_look and rep.imported == 0 and rep.history_top == 0
        assert await db.sent_posts(1) == [] and await db.get_history(1) is None
        await db.close()

    run(go())


# ===================================================================================== a long channel, in pieces
def test_a_long_channel_is_read_over_several_looks_and_nothing_twice(tmp_path, monkeypatch):
    async def go():
        chan, db, ch = await world(tmp_path, history(450), save=False)
        ticking(monkeypatch)
        rep = await sync_channel(chan, db, ch, opts(history_seconds=2.5))  # two chunks of 100 fit into 2.5 "seconds"
        assert rep.imported == 200 and not rep.history_done and (rep.history_to, rep.history_top) == (200, 450)
        h = await db.get_history(1)
        assert (h.top, h.next_id, h.imported, h.done) == (450, 201, 200, False)

        rep = await sync_channel(chan, db, ch, opts(history_seconds=2.5))
        assert rep.imported == 200 and rep.history_to == 400 and not rep.history_done and rep.first_look is False
        rep = await sync_channel(chan, db, ch, opts(history_seconds=2.5))
        assert rep.imported == 50 and rep.history_done
        h = await db.get_history(1)
        assert (h.next_id, h.imported, h.done) == (451, 450, True) and h.finished_at is not None
        assert [p.message_id for p in await db.sent_posts(1)] == list(range(1, 451))
        again = await sync_channel(chan, db, ch, opts(history_seconds=2.5))
        assert again.imported == 0 and again.history_top == 0  # finished for good
        await db.close()

    run(go())


def test_a_long_job_stops_the_reading_and_it_goes_on_later(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, history(250), save=False)
        long_job = {"running": False}

        async def after_each_piece(rep):
            long_job["running"] = True  # a /repost starts while the old posts are being read

        rep = await sync_channel(chan, db, ch, opts(), busy=lambda: long_job["running"], progress=after_each_piece)
        assert rep.busy and rep.imported == 100 and not rep.history_done
        long_job["running"] = False
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.imported == 150 and rep.history_done and not rep.busy
        assert len(await db.sent_posts(1)) == 250
        await db.close()

    run(go())


def test_the_reading_waits_for_a_message_that_is_too_fresh(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, history(5), save=False)
        late = foreign(chan, "sent a few seconds before the first look", age=5)  # id 6: the bot may still be saving it
        rep = await sync_channel(chan, db, ch, opts(grace=120))
        assert rep.imported == 5 and not rep.history_done and rep.history_to == 5
        assert (await db.get_history(1)).next_id == 6 and await db.find_by_message(1, late.id) is None
        late.date = OLD  # time passes
        rep = await sync_channel(chan, db, ch, opts(grace=120))
        assert rep.imported == 1 and rep.history_done and (await db.find_by_message(1, late.id)).text.startswith("sent a few")
        await db.close()

    run(go())


def test_a_channel_the_bot_followed_before_the_import_existed_gets_its_older_posts_too(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, history(10), save=False)
        await db.save_sync(1, last_top=10, last_pts=10, deep=True)  # what the earlier version stored at its first look
        later = foreign(chan, "made after that look")  # id 11
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.first_look is False and rep.adopted == [later.id] and rep.imported == 10 and rep.history_done
        assert (await db.get_history(1)).top == 10  # the new post belongs to the new posts, not to the older ones
        assert len(await db.sent_posts(1)) == 11
        await db.close()

    run(go())


def test_an_empty_channel_has_nothing_to_read(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {}, save=False)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.first_look and rep.imported == 0 and rep.history_top == 0  # nothing to say about the older posts
        h = await db.get_history(1)
        assert h.done and h.top == 0
        await db.close()

    run(go())


def test_only_chunks_of_a_hundred_ids_are_asked_for_and_none_twice(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, history(230), save=False)
        asked = []
        real = chan.get_messages

        async def spy(peer, ids=None):
            asked.append((min(ids), max(ids), len(ids)))
            return await real(peer, ids=ids)

        chan.get_messages = spy
        await sync_channel(chan, db, ch, opts())
        assert all(n <= 100 for _, _, n in asked)
        for piece in ((1, 100, 100), (101, 200, 100), (201, 230, 30)):
            assert asked.count(piece) == 1, (piece, asked)
        await db.close()

    run(go())


# =================================================================================== the owner decides what comes back
def test_forgotten_posts_stay_forgotten_even_after_a_restart(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        p = await app.db.find_by_message(1, 2)
        await app.press(f"dly:{p.id}:b")  # "Only forget it in the bot"
        assert await app.db.ignored_ids(1) == {2}
        app.ctx.sync_ignore.clear()  # a restart forgets what was only in memory
        await app.text("/sync")
        assert await app.db.find_by_message(1, 2) is None
        await app.text("/sync --history")
        assert await app.db.find_by_message(1, 2) is None
        assert 2 in app.tg.msgs  # it is still in the channel, the bot just does not list it
        await app.db.close()

    run(go())


def test_sync_history_reads_the_older_posts_again(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        await app.text("/sync")
        gone = await app.db.find_by_message(1, 3)
        await app.db.delete_post(gone.id)  # removed from My posts without the "forget" mark (a mistake, a lost backup ...)
        await app.text("/sync")  # the older posts were read already: not again
        assert await app.db.find_by_message(1, 3) is None
        await app.text("/sync --history")
        text = norm(app.out.last_text)
        assert "1 older post(s)" in text and "added to My posts" in text
        back = await app.db.find_by_message(1, 3)
        assert back is not None and back.source == "adopted" and back.text == "third"
        await app.db.close()

    run(go())


def test_the_import_can_be_switched_off_but_sync_history_still_works(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=[], import_old_posts=False)
        app.tg.msgs.update({i: old_msg(i, f"older {i}") for i in (1, 2, 3)})
        app.tg._top = app.tg._pts = 3
        await app.text("/sync")
        assert await app.db.sent_posts(1) == [] and await app.db.get_history(1) is None
        await app.text("/sync --history")
        assert len(await app.db.sent_posts(1)) == 3 and norm(app.out.last_text).count("3 older post(s)") == 1
        await app.db.close()

    run(go())


def test_the_timed_check_reads_for_a_limited_time_and_sync_reads_all_of_it(tmp_path, monkeypatch):
    async def go():
        app = await make_app(tmp_path, posts=[])
        app.tg.msgs.update(history(250))
        app.tg._top = app.tg._pts = 250
        ticking(monkeypatch)
        sy = app.ctx.syncer
        sy.history_seconds = 1.5
        await sy.run_all()  # the timed check: 100 ids
        assert len(await app.db.sent_posts(1)) == 100
        await app.text("/sync")  # nobody waits for a time limit here
        assert len(await app.db.sent_posts(1)) == 250
        assert "150 older post(s)" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_the_live_look_does_not_read_old_posts(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=[])
        app.tg.msgs.update(history(10))
        app.tg._top = app.tg._pts = 10
        sy = app.ctx.syncer
        await sy._process({1: {1, 2}}, {})  # an edit was noticed: only those ids are looked at
        assert await app.db.sent_posts(1) == [] and await app.db.get_history(1) is None
        await sy._process({1: set(range(1, 400))}, {})  # a burst of edits: one look at the whole channel, still no history
        assert await app.db.get_history(1) is None
        await app.db.close()

    run(go())


# ===================================================================================== what the sync reports
def test_sync_reports_the_older_posts_that_came_in_and_the_ones_still_to_come(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=[])
        app.tg.msgs.update(history(5))
        app.tg._top = app.tg._pts = 5
        late = app.tg._new("a few seconds old", None, None)  # id 6
        late.out = False
        late.date = utcnow() - datetime.timedelta(seconds=5)
        await app.text("/sync")
        text = norm(app.out.last_text)
        assert "First look" in text and "Older posts: 5 added so far, read up to message 5 of 6" in text
        assert "send /sync again" in text and "Everything matches" not in text
        late.date = OLD
        await app.text("/sync")
        text = norm(app.out.last_text)
        assert "1 older post(s), sent before the bot was added → added to My posts" in text
        await app.text("/sync")
        assert "Everything matches." in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_sync_says_when_there_was_nothing_to_add(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        await app.text("/sync")
        assert "The older posts were looked through: nothing to add" in norm(app.out.last_text)
        await app.db.close()

    run(go())


# ============================================================================= deleted in the channel -> gone here
def test_a_post_deleted_in_the_channel_takes_what_hangs_on_it_with_it(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        later = utcnow() + datetime.timedelta(hours=3)
        await app.db.schedule_deletes(
            [dict(channel_id=1, message_id=m, delete_at=later, group_id="g", created_by=1) for m in (2, 3)]
        )
        await app.db.ignore_post(1, 2)
        await app.text("/sync")  # the first look
        del app.tg.msgs[2]
        app.tg._pts += 1
        await app.text("/sync")
        assert "1 deleted in the channel → removed from My posts (message 2)" in norm(app.out.last_text)
        assert await app.db.find_by_message(1, 2) is None
        assert [r.message_id for r in await app.db.pending_deletes()] == [3]  # the other post keeps its timer
        assert await app.db.ignored_ids(1) == set()
        await app.db.close()

    run(go())


def test_a_delete_the_bot_hears_about_while_it_runs_takes_the_same_with_it(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        later = utcnow() + datetime.timedelta(hours=3)
        await app.db.schedule_deletes([dict(channel_id=1, message_id=3, delete_at=later, group_id="g", created_by=1)])
        del app.tg.msgs[3]
        await app.raw(delete_update(1, [3]))
        assert await app.db.find_by_message(1, 3) is None
        assert await app.db.pending_deletes() == [] and await app.db.pending_deletes(status="failed") == []
        assert len(await app.db.sent_posts(1)) == 2
        await app.db.close()

    run(go())


def test_forgetting_posts_counts_only_the_posts(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three_posts_dict())
        later = utcnow() + datetime.timedelta(hours=1)
        await db.schedule_deletes([dict(channel_id=1, message_id=9, delete_at=later, group_id="g", created_by=1)])  # no post there
        assert await db.forget_posts(1, [9, 2]) == 1
        assert await db.pending_deletes() == []
        assert await db.forget_posts(1, []) == 0
        await db.close()

    run(go())


def three_posts_dict():
    return {m.id: m for m in three_posts()}


# ================================================================================================== bulk saving
def test_saving_many_posts_at_once_survives_a_post_saved_by_someone_else_a_moment_earlier(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {}, save=False)
        items = [(i, {"text": f"post {i}", "entities": [], "buttons": []}, OLD) for i in (1, 2, 3)]
        fired = []

        def clash(session, flush_context, instances):
            if fired:
                return
            fired.append(1)
            first = next(o for o in session.new if isinstance(o, Post))
            session.add(Post(channel_id=first.channel_id, message_id=first.message_id, status="sent", source="bot"))

        event.listen(SyncSession, "before_flush", clash)  # the same message id twice in one save: the database refuses
        try:
            made = await db.adopt_many(1, items)
        finally:
            event.remove(SyncSession, "before_flush", clash)
        assert fired and sorted(made) == [1, 2, 3]  # saved one by one instead
        assert [p.text for p in await db.sent_posts(1)] == ["post 1", "post 2", "post 3"]
        assert await db.adopt_many(1, items) == []  # and a second time there is nothing new
        assert await db.adopt_many(1, []) == []
        await db.close()

    run(go())
