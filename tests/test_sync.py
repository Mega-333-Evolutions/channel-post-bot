"""The sync engine: My posts follows what channel admins do outside the bot (deleted, edited and new posts, service messages)."""
import asyncio
import datetime

from telethon import errors, types

from app.db import as_utc, utcnow
from app.servicemsg import remove
from app.sync_engine import (
    SyncOptions,
    diff_post,
    find_top,
    live_fields,
    message_supported,
    sync_channel,
    sync_ids,
)
from app.tgutil import media_info

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, make_msg, markup, photo_media, service_msg, url_btn, video_media

OLD = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
FAST = dict(pause=0)


def old_msg(i, text="", **kw):
    m = make_msg(i, text, **kw)
    m.date = OLD
    return m


def foreign(chan, text="", *, age=86400, **kw):
    """A post somebody else made in the channel (not out)."""
    m = chan._new(text, kw.pop("entities", None), kw.pop("markup", None), kw.pop("media", None))
    m.out = False
    m.date = utcnow() - datetime.timedelta(seconds=age)
    return m


async def saved(db, m, **extra):
    """The bot's saved copy of live message `m`, exactly as the bot would have saved it a day ago."""
    fields = dict(
        channel_id=1, message_id=m.id, status="sent", source="bot", created_by=99, link_preview=False,
        created_at=OLD, updated_at=OLD, sent_at=OLD, **live_fields(m),
    )
    fields.update(extra)
    return await db.create_post(**fields)


async def world(tmp_path, msgs, *, save=True):
    chan = FakeChannelClient(msgs, username="chan")
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Chan", "chan", 99)
    ch = await db.get_channel(1)
    if save:
        for m in msgs.values():
            await saved(db, m)
    return chan, db, ch


def run(coro):
    return asyncio.run(coro)


def opts(**kw):
    return SyncOptions(**{**FAST, "grace": 0, **kw})


def three():
    return {
        1: old_msg(1, "first"),
        2: old_msg(2, "second", markup=markup([url_btn("Watch", "https://t.me/a/1")])),
        3: old_msg(3, "third"),
    }


# ============================================================================================ comparing one post
def test_a_post_that_is_as_saved_has_no_difference():
    m = old_msg(1, "hello", markup=markup([url_btn("Go", "https://x.org")]))
    p = SaveLike(m)
    d = diff_post(p, m)
    assert d.fields == {} and not d.buttons_missing and not d.quiet


class SaveLike:
    """Anything with the Post fields."""

    def __init__(self, m, **kw):
        for k, v in live_fields(m).items():
            setattr(self, k, v)
        self.link_preview = False
        for k, v in kw.items():
            setattr(self, k, v)


def test_text_formatting_and_buttons_that_changed_are_found():
    m = old_msg(1, "hello world", entities=[types.MessageEntityBold(0, 5)], markup=markup([url_btn("Go", "https://x.org")]))
    p = SaveLike(m, text="hello", entities=[], buttons=[[{"t": "Go", "u": "https://old.org"}]])
    d = diff_post(p, m)
    assert d.fields["text"] == "hello world"
    assert d.fields["entities"] == [{"k": "bold", "o": 0, "l": 5}]
    assert d.fields["buttons"] == [[{"t": "Go", "u": "https://x.org"}]]
    assert not d.quiet and not d.buttons_missing


def test_a_keyboard_that_is_not_shown_never_wipes_the_saved_buttons():
    m = old_msg(1, "hello")  # the channel shows no keyboard
    p = SaveLike(m, buttons=[[{"t": "Go", "u": "https://x.org"}]])
    d = diff_post(p, m)
    assert d.buttons_missing and "buttons" not in d.fields


def test_only_spaces_around_the_text_is_a_quiet_correction():
    m = old_msg(1, "hello")
    d = diff_post(SaveLike(m, text="hello \n"), m)
    assert d.fields == {"text": "hello"} and d.quiet


def test_media_is_compared_by_its_id_not_by_its_file_reference():
    m = old_msg(1, "pic", media=photo_media(11))
    p = SaveLike(m)
    other_fetch = old_msg(1, "pic", media=photo_media(11))
    other_fetch.media.photo.file_reference = b"a-fresh-reference"
    assert diff_post(p, other_fetch).fields == {}  # same photo, new file reference: nothing changed
    replaced = old_msg(1, "pic", media=photo_media(12))
    d = diff_post(p, replaced)
    assert d.fields["media_kind"] == "photo" and d.fields["media_file_id"] == media_info(replaced)[1]


def test_media_that_seems_to_have_vanished_is_not_forgotten():
    with_media = old_msg(1, "pic", media=photo_media(11))
    p = SaveLike(with_media)
    stripped = old_msg(1, "pic")  # Telegram showed it without the photo this time
    assert diff_post(p, stripped).fields == {}


def test_what_the_bot_can_hold():
    assert message_supported(old_msg(1, "text"))
    assert message_supported(old_msg(1, "", media=photo_media()))
    assert message_supported(old_msg(1, "", media=video_media()))
    assert not message_supported(old_msg(1, ""))  # a poll, a sticker ...
    assert not message_supported(old_msg(1, "  \n "))


# ================================================================================================= the first look
def test_the_first_look_only_notes_where_the_channel_stands(tmp_path):
    async def go():
        msgs = three()
        chan, db, ch = await world(tmp_path, msgs, save=False)  # nothing saved: the channel has history only
        for i in (4, 5):
            chan.msgs[i] = old_msg(i, f"older post {i}")
        chan._top = chan._pts = 5
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.first_look and rep.error is None
        assert rep.adopted == [] and rep.deleted == [] and rep.edited == []  # the history is not imported
        assert await db.list_posts(1, None, 0, 50) == ([], 0)
        state = await db.get_sync(1)
        assert (state.last_top, state.last_pts, state.deferred) == (5, 5, 0)
        await db.close()

    run(go())


def test_a_channel_without_any_message_is_fine(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {}, save=False)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.first_look and rep.error is None and rep.adopted == []
        again = await sync_channel(chan, db, ch, opts())
        assert again.error is None and again.changed is False
        await db.close()

    run(go())


def test_the_first_look_finds_the_newest_message_below_a_long_run_of_deleted_ids(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {1: old_msg(1, "a"), 2: old_msg(2, "b")}, save=False)
        chan._top, chan._pts = 2, 1000  # a thousand edits and deletes since
        top = await find_top(chan, types.InputPeerChannel(1, 5), 1000, 0, pause=0)
        assert top == 2
        # the newest saved post is a floor: looking stops there
        assert await find_top(chan, types.InputPeerChannel(1, 5), 1000, 2, pause=0) == 2
        await db.close()

    run(go())


# ============================================================================================= saved posts vs channel
def test_a_post_deleted_in_the_channel_leaves_my_posts(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        await sync_channel(chan, db, ch, opts())  # the first look
        del chan.msgs[2]
        chan._pts += 1
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.deleted == [2] and rep.checked == 3
        assert sorted(p.message_id for p in await db.sent_posts(1)) == [1, 3]
        await db.close()

    run(go())


def test_a_post_edited_in_the_channel_is_updated_here(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        m2 = chan.msgs[2]
        m2.message = "second, edited by an admin"
        m2.entities = [types.MessageEntityItalic(0, 6)]
        m2.reply_markup = markup([url_btn("Watch now", "https://t.me/new/9")])
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.edited == [2] and rep.deleted == [] and rep.adopted == []
        p = await db.find_by_message(1, 2)
        assert p.text == "second, edited by an admin"
        assert p.entities == [{"k": "italic", "o": 0, "l": 6}]
        assert p.buttons == [[{"t": "Watch now", "u": "https://t.me/new/9"}]]
        again = await sync_channel(chan, db, ch, opts())
        assert again.edited == [] and again.changed is False  # nothing left to record
        await db.close()

    run(go())


def test_a_missing_keyboard_is_reported_and_the_saved_buttons_stay(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        chan.msgs[2].reply_markup = None  # the channel shows no keyboard any more
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.buttons_missing == [2] and rep.edited == []
        assert (await db.find_by_message(1, 2)).buttons == [[{"t": "Watch", "u": "https://t.me/a/1"}]]
        await db.close()

    run(go())


def test_posts_the_bot_touched_a_moment_ago_are_left_for_later(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        await db.update_post((await db.find_by_message(1, 1)).id, text="just edited through the bot")  # updated_at = now
        chan.msgs[1].message = "an admin's text"
        rep = await sync_channel(chan, db, ch, opts(grace=120))
        assert rep.young == [1] and rep.edited == [] and rep.checked == 2
        assert (await db.find_by_message(1, 1)).text == "just edited through the bot"
        await db.close()

    run(go())


def test_if_every_saved_post_looks_deleted_nothing_is_removed(tmp_path):
    async def go():
        msgs = {i: old_msg(i, f"post {i}") for i in range(1, 8)}
        chan, db, ch = await world(tmp_path, msgs)
        chan.msgs.clear()  # e.g. the bot lost access and Telegram shows nothing
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.deleted == [] and rep.anomaly and "All 7" in rep.anomaly
        assert len(await db.sent_posts(1)) == 7
        await db.close()

    run(go())


def test_one_or_two_posts_can_be_deleted_even_when_that_is_all_there_is(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {1: old_msg(1, "a"), 2: old_msg(2, "b")})
        chan.msgs.clear()
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.deleted) == [1, 2] and rep.anomaly is None
        await db.close()

    run(go())


def test_a_change_made_through_the_bot_while_the_channel_was_read_wins(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        chan.msgs[3].message = "an admin's text"
        pid = (await db.find_by_message(1, 3)).id
        real_get = chan.get_messages

        async def get_while_the_owner_edits(peer, ids=None):
            res = await real_get(peer, ids=ids)
            await db.update_post(pid, text="the owner's newer text")  # happens between reading and saving
            return res

        chan.get_messages = get_while_the_owner_edits
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.edited == [] and rep.changed_meanwhile == 1
        assert (await db.find_by_message(1, 3)).text == "the owner's newer text"
        await db.close()

    run(go())


# ================================================================================================= new posts
async def looked(tmp_path, msgs=None):
    chan, db, ch = await world(tmp_path, msgs or three())
    first = await sync_channel(chan, db, ch, opts())  # the first look sets the mark
    assert first.first_look
    return chan, db, ch


def test_a_new_post_made_outside_the_bot_is_added(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        m = foreign(chan, "posted from the Telegram app", entities=[types.MessageEntityBold(0, 6)],
                    markup=markup([url_btn("Open", "https://example.org/x")]))
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.adopted == [m.id] and rep.first_look is False
        p = await db.find_by_message(1, m.id)
        assert (p.source, p.status, p.created_by) == ("adopted", "sent", 0)
        assert p.text == "posted from the Telegram app"
        assert p.entities == [{"k": "bold", "o": 0, "l": 6}]
        assert p.buttons == [[{"t": "Open", "u": "https://example.org/x"}]]
        assert p.media_kind is None and p.link_preview is False
        assert as_utc(p.sent_at) == as_utc(m.date)
        state = await db.get_sync(1)
        assert state.last_top == m.id
        again = await sync_channel(chan, db, ch, opts())
        assert again.adopted == []  # not saved twice
        assert len(await db.sent_posts(1)) == 4
        await db.close()

    run(go())


def test_new_media_posts_keep_their_media(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        m = foreign(chan, "caption", media=photo_media(31))
        await sync_channel(chan, db, ch, opts())
        p = await db.find_by_message(1, m.id)
        assert p.media_kind == "photo" and p.media_file_id.startswith("tl:") and p.text == "caption"
        await db.close()

    run(go())


def test_the_bots_own_posts_that_are_saved_are_not_picked_up_again(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        m = chan._new("made with the bot", None, None)  # out=True, like the bot's own post
        m.date = utcnow() - datetime.timedelta(minutes=10)
        mine = await db.create_post(channel_id=1, message_id=m.id, status="sent", source="bot", created_by=99,
                                    sent_at=OLD, created_at=OLD, updated_at=OLD, **live_fields(m))
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.adopted == [] and rep.deleted == []
        assert (await db.find_by_message(1, m.id)).id == mine.id and mine.source == "bot"
        await db.close()

    run(go())


def test_a_message_that_appeared_a_moment_ago_waits_for_the_next_look(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        m = foreign(chan, "brand new", age=1)
        rep = await sync_channel(chan, db, ch, opts(grace=120))
        assert rep.adopted == [] and rep.young_new == [m.id]
        state = await db.get_sync(1)
        assert state.last_top == 3 and state.deferred == 1  # the mark stays below it
        m.date = utcnow() - datetime.timedelta(minutes=10)  # time passes
        rep = await sync_channel(chan, db, ch, opts(grace=120))
        assert rep.adopted == [m.id] and rep.young_new == []
        state = await db.get_sync(1)
        assert state.last_top == m.id and state.deferred == 0
        await db.close()

    run(go())


def test_a_quiet_channel_costs_no_reading_at_all(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        before = len(chan.requests)
        reads = []
        real_get = chan.get_messages

        async def counting(peer, ids=None):
            reads.append(list(ids))
            return await real_get(peer, ids=ids)

        chan.get_messages = counting
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.adopted == [] and rep.scanned_to == 3  # only the saved posts were compared (ids 1-3)
        assert reads == [[1, 2, 3]]  # nothing was read beyond them: not a single event since the last look
        assert len(chan.requests) - before == 1  # just the channel counter
        await db.close()

    run(go())


def test_messages_of_a_kind_the_bot_cannot_hold_are_left_alone(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        foreign(chan, "")  # a poll or a sticker: no text, no supported media
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.adopted == [] and rep.unsupported == 1
        await db.close()

    run(go())


def test_a_post_the_owner_made_the_bot_forget_is_not_brought_back(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        m = foreign(chan, "a post that cannot be deleted")
        rep = await sync_channel(chan, db, ch, opts(ignore={(1, m.id)}))
        assert rep.adopted == []
        await db.close()

    run(go())


def test_a_dead_tail_longer_than_the_quick_look_needs_a_deep_look(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        chan._top, chan._pts = 3, 900  # lots of events happened: the counter is far above the newest message
        first = await sync_channel(chan, db, ch, opts())
        assert (await db.get_sync(1)).last_top == 3 and first.first_look
        chan._top = 600  # 597 ids went to messages that were deleted, then a new post appears
        chan._pts = 901
        m = foreign(chan, "after a very long run of deleted ids")
        assert m.id == 601
        quick = await sync_channel(chan, db, ch, opts())
        assert quick.adopted == []  # the quick look reads 300 ids beyond the likely end and stops
        deep = await sync_channel(chan, db, ch, opts(deep=True))
        assert deep.adopted == [601]
        assert (await db.get_sync(1)).last_top == 601
        await db.close()

    run(go())


def test_the_automatic_looks_go_deep_once_a_day(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        chan._top, chan._pts = 3, 900
        await sync_channel(chan, db, ch, opts())
        chan._top, chan._pts = 600, 901
        m = foreign(chan, "hidden behind a long dead tail")
        assert (await sync_channel(chan, db, ch, opts())).adopted == []
        # a day later the same automatic look reads every possible id
        old = utcnow() - datetime.timedelta(hours=25)
        state = await db.get_sync(1)
        await db.save_sync(1, last_top=state.last_top, last_pts=state.last_pts, deep=False)
        async with db.Session() as s:
            row = await s.get(type(state), 1)
            row.last_deep = old
            await s.commit()
        assert (await sync_channel(chan, db, ch, opts())).adopted == [m.id]
        await db.close()

    run(go())


# ======================================================================================== service messages
def test_service_messages_that_appeared_are_deleted_by_the_look(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        chan.msgs[4] = service_msg(4)
        chan.msgs[5] = types.MessageService(id=5, peer_id=types.PeerChannel(1), date=OLD, action=types.MessageActionChatEditTitle("New"))
        chan._top, chan._pts = 5, 5
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.services_deleted == 2 and rep.services_failed == 0
        assert 4 not in chan.msgs and 5 not in chan.msgs and sorted(chan.msgs) == [1, 2, 3]
        assert rep.adopted == []
        await db.close()

    run(go())


def test_service_messages_stay_when_the_switch_is_off(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        chan.msgs[4] = service_msg(4)
        chan._top, chan._pts = 4, 4
        rep = await sync_channel(chan, db, ch, opts(delete_services=False))
        assert rep.services_deleted == 0 and 4 in chan.msgs
        await db.close()

    run(go())


def test_a_service_message_the_bot_may_not_delete_is_counted(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        chan.msgs[4] = service_msg(4)
        chan._top, chan._pts = 4, 4
        chan.undeletable.add(4)
        rep = await sync_channel(chan, db, ch, opts())
        assert (rep.services_deleted, rep.services_failed) == (0, 1) and rep.services_error
        await db.close()

    run(go())


def test_clean_sweeps_the_whole_history_for_old_service_messages(tmp_path):
    async def go():
        msgs = {1: old_msg(1, "a"), 2: service_msg(2), 3: old_msg(3, "b"), 4: service_msg(4), 5: old_msg(5, "c")}
        chan, db, ch = await world(tmp_path, {i: m for i, m in msgs.items() if not isinstance(m, types.MessageService)})
        for i in (2, 4):
            chan.msgs[i] = msgs[i]
        rep = await sync_channel(chan, db, ch, opts(clean=True))
        assert rep.services_deleted == 2 and sorted(chan.msgs) == [1, 3, 5]
        assert len(await db.sent_posts(1)) == 3  # My posts untouched
        await db.close()

    run(go())


def test_clean_hands_what_the_bot_cannot_delete_to_the_userbot(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {1: old_msg(1, "a")})
        chan.msgs[2] = service_msg(2)
        chan._top = chan._pts = 2
        chan.undeletable.add(2)
        asked = []

        async def userbot_delete(ids):
            asked.append(list(ids))
            chan.msgs.pop(2, None)

        rep = await sync_channel(chan, db, ch, opts(clean=True, fallback=userbot_delete))
        assert asked == [[2]] and rep.services_deleted == 1 and rep.services_failed == 0
        await db.close()

    run(go())


# ============================================================================================= errors and busy
def test_a_channel_the_bot_cannot_read_is_reported_not_raised(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        chan.fail["GetFullChannelRequest"] = errors.ChannelPrivateError(request=None)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.error and "ChannelPrivateError" in rep.error
        assert len(await db.sent_posts(1)) == 3
        chan.fail.clear()
        chan.fail["GetMessagesRequest"] = errors.ChannelPrivateError(request=None)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.error and len(await db.sent_posts(1)) == 3
        await db.close()

    run(go())


def test_a_long_job_that_starts_cuts_the_look_short(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path)
        m = foreign(chan, "new while a repost runs")
        del chan.msgs[2]
        chan._pts += 1
        rep = await sync_channel(chan, db, ch, opts(), busy=lambda: True)
        assert rep.busy and rep.deleted == [] and rep.adopted == []
        assert len(await db.sent_posts(1)) == 3
        rep = await sync_channel(chan, db, ch, opts())  # the job is over
        assert rep.deleted == [2] and rep.adopted == [m.id]
        await db.close()

    run(go())


# =================================================================================== looking at given ids only
def test_sync_ids_handles_edits_deletes_new_posts_and_notices(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        chan.msgs[1].message = "edited"
        del chan.msgs[3]
        new = foreign(chan, "added")
        chan.msgs[9] = service_msg(9)
        chan._top = 9
        ids = [1, 3, new.id, 9, 2]
        rep = await sync_ids(chan, db, ch, ids, opts(), adopt_ids={new.id, 9})
        assert rep.edited == [1] and rep.deleted == [3] and rep.adopted == [new.id]
        assert rep.services_deleted == 1 and 9 not in chan.msgs
        assert (await db.find_by_message(1, 2)).text == "second"
        await db.close()

    run(go())


def test_an_edit_of_a_post_the_bot_never_saved_is_history_and_ignored(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        old = old_msg(7, "an old post, edited today")
        chan.msgs[7] = old
        chan._top = 7
        rep = await sync_ids(chan, db, ch, [7], opts(), adopt_ids=set())
        assert rep.adopted == [] and await db.find_by_message(1, 7) is None
        await db.close()

    run(go())


def test_sync_ids_reports_what_is_too_fresh(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, three())
        new = foreign(chan, "right now", age=0)
        await db.update_post((await db.find_by_message(1, 1)).id, text="saved a second ago")
        chan.msgs[1].message = "different"
        rep = await sync_ids(chan, db, ch, [1, new.id], opts(grace=60), adopt_ids={new.id})
        assert rep.young == [1] and rep.young_new == [new.id] and rep.adopted == [] and rep.edited == []
        await db.close()

    run(go())


# ================================================================================================ the database
def test_adopting_a_message_twice_saves_it_once(tmp_path):
    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Chan", "chan", 99)
        p, created = await db.adopt_message(1, 50, {"text": "x"}, sent_at=OLD)
        assert created and p.source == "adopted"
        again, created = await db.adopt_message(1, 50, {"text": "y"})
        assert again is None and created is False
        assert (await db.find_by_message(1, 50)).text == "x"
        await db.close()

    run(go())


def test_a_reposted_copy_takes_the_place_of_a_post_the_sync_saved_first(tmp_path):
    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Chan", "chan", 99)
        await db.adopt_message(1, 50, {"text": "picked up by the sync"})
        await db.create_migration("m1", 1, old=None, new=None, include_typed=False, include_posts=False, first_id=1, last_id=9, partial=False, user_id=99)
        await db.record_repost("m1", 1, [{"old_id": 1, "new_id": 50, "post": {"text": "the copy"}}], 99)
        p = await db.find_by_message(1, 50)
        assert p.text == "the copy" and p.source == "bot"
        await db.close()

    run(go())


def test_publishing_takes_the_place_of_a_post_the_sync_saved_first(tmp_path):
    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Chan", "chan", 99)
        await db.adopt_message(1, 60, {"text": "picked up first"})
        draft = await db.create_post(channel_id=1, status="draft", text="my draft", created_by=99)
        assert await db.release_slot(1, 60, keep_pid=draft.id) == 1
        sent = await db.update_post(draft.id, status="sent", message_id=60)
        assert sent.message_id == 60 and (await db.find_by_message(1, 60)).text == "my draft"
        await db.close()

    run(go())


def test_the_sync_update_only_applies_to_an_unchanged_post(tmp_path):
    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Chan", "chan", 99)
        post = await db.create_post(channel_id=1, message_id=5, status="sent", text="v1", created_by=99)
        seen = (await db.get_post(post.id)).updated_at
        assert await db.sync_update_post(post.id, seen, {"text": "v2"}) is True
        assert await db.sync_update_post(post.id, seen, {"text": "v3"}) is False  # it changed since `seen`
        assert (await db.get_post(post.id)).text == "v2"
        await db.close()

    run(go())


def test_servicemsg_remove_reports_refusals_instead_of_raising(tmp_path):
    async def go():
        chan = FakeChannelClient({1: service_msg(1), 2: service_msg(2)})
        peer = types.InputPeerChannel(1, 5)
        ok = await remove(chan, peer, [1, 2])
        assert (ok.deleted, ok.failed, ok.error) == (2, 0, None)
        chan.msgs[3] = service_msg(3)
        chan.fail["DeleteMessagesRequest"] = errors.ChatAdminRequiredError(request=None)
        bad = await remove(chan, peer, [3])
        assert bad.failed == 1 and bad.error_name == "ChatAdminRequiredError"

    run(go())
