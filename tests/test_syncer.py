"""The live side of the sync (what Telegram's updates say), the timed check, service-message deletion and /sync."""
import asyncio
import datetime

from telethon import errors, types

from app.db import utcnow
from app.syncer import Syncer

from .fakes import FakeChannelClient, markup, photo_media, url_btn
from .harness import App, norm
from .test_sync import OLD, foreign, old_msg, saved

NOW = datetime.datetime.now(datetime.timezone.utc)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------ set-up
async def make_app(tmp_path, *, posts=(), **cfg):
    """Channel 1 "Anime Channel" (the app's own fake) with `posts` saved the way the bot saved them a day ago."""
    app = App(tmp_path, edit_delay=0, **cfg)
    await app.start()
    app.tg.username, app.tg.title = "animech", "Anime Channel"
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    for m in posts:
        app.tg.msgs[m.id] = m
        app.tg._top = app.tg._pts = max(app.tg._top, m.id)
        await saved(app.db, m)
    sy = app.ctx.syncer
    sy.debounce, sy.max_debounce, sy.lock_wait, sy.rt_grace, sy.pause, sy.start_delay = 0.01, 0.2, 0.02, 0, 0, 0.01
    return app


def three():
    return [
        old_msg(1, "first"),
        old_msg(2, "second", markup=markup([url_btn("Watch", "https://t.me/a/1")])),
        old_msg(3, "third"),
    ]


def new_update(m):
    return types.UpdateNewChannelMessage(message=m, pts=m.id, pts_count=1)


def edit_update(m):
    return types.UpdateEditChannelMessage(message=m, pts=m.id, pts_count=1)


def delete_update(cid, ids):
    return types.UpdateDeleteChannelMessages(channel_id=cid, messages=list(ids), pts=0, pts_count=len(ids))


def notice(i, action=None, cid=1):
    return types.MessageService(
        id=i, peer_id=types.PeerChannel(cid), date=NOW, action=action or types.MessageActionPinMessage()
    )


def dms(app):
    """Texts the bot sent to people (not to channels)."""
    return [e[2] for e in app.tg.sent if not isinstance(e[1], types.InputPeerChannel)]


# ============================================================================================ service messages
def test_every_kind_of_notice_is_deleted_the_moment_it_appears(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        actions = [
            types.MessageActionChatEditTitle("A new name"),
            types.MessageActionChatEditPhoto(photo=types.PhotoEmpty(0)),
            types.MessageActionChatDeletePhoto(),
            types.MessageActionPinMessage(),
            types.MessageActionGroupCall(call=types.InputGroupCall(1, 2)),
            types.MessageActionHistoryClear(),
        ]
        for n, action in enumerate(actions, start=10):
            app.tg.msgs[n] = notice(n, action)
            await app.raw(new_update(app.tg.msgs[n]))
        assert [i for i in app.tg.msgs if i >= 10] == []
        assert sorted(app.tg.msgs) == [1, 2, 3]  # posts are never touched
        assert len(await app.db.sent_posts(1)) == 3
        await app.db.close()

    run(go())


def test_notices_stay_when_the_switch_is_off(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three(), delete_service_messages=False)
        app.tg.msgs[10] = notice(10)
        await app.raw(new_update(app.tg.msgs[10]))
        assert 10 in app.tg.msgs
        await app.db.close()

    run(go())


def test_notices_in_channels_that_are_not_connected_are_left_alone(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        other = app.tg.add_channel(FakeChannelClient({5: notice(5, cid=2)}, username="other", cid=2, title="Other"))
        before = len(app.tg.requests) + len(other.requests)
        await app.raw(new_update(other.msgs[5]))
        assert 5 in other.msgs and len(app.tg.requests) + len(other.requests) == before  # not even a request
        # a channel that was removed from the bot counts as not connected too
        await app.db.set_channel_active(1, False)
        app.ctx.syncer.forget_cache()
        app.tg.msgs[10] = notice(10)
        await app.raw(new_update(app.tg.msgs[10]))
        assert 10 in app.tg.msgs
        await app.db.close()

    run(go())


def test_the_owner_is_told_once_when_the_bot_may_not_delete(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        app.tg.fail["DeleteMessagesRequest"] = errors.ChatAdminRequiredError(request=None)
        for i in (10, 11, 12):
            app.tg.msgs[i] = notice(i)
            await app.raw(new_update(app.tg.msgs[i]))
        texts = [norm(t) for t in dms(app)]
        assert len(texts) == 1 and "Anime Channel" in texts[0] and "Delete messages" in texts[0]
        assert "DELETE_SERVICE_MESSAGES" in texts[0]
        assert {10, 11, 12} <= set(app.tg.msgs)
        # a day later the same problem is worth saying again
        app.ctx.syncer.tell_every = 0
        await app.raw(new_update(app.tg.msgs[10]))
        assert len(dms(app)) == 2
        await app.db.close()

    run(go())


def test_other_refusals_are_only_logged(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        app.tg.fail["DeleteMessagesRequest"] = errors.MessageDeleteForbiddenError(request=None)
        app.tg.msgs[10] = notice(10)
        await app.raw(new_update(app.tg.msgs[10]))
        assert dms(app) == [] and 10 in app.tg.msgs
        await app.db.close()

    run(go())


def test_notices_are_deleted_even_when_the_automatic_sync_is_switched_off(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three(), sync_interval_minutes=0)
        app.tg.msgs[10] = notice(10)
        await app.raw(new_update(app.tg.msgs[10]))
        assert 10 not in app.tg.msgs
        await app.db.close()

    run(go())


def test_notices_are_deleted_while_a_long_job_runs(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        async with app.ctx.lock:
            app.tg.msgs[10] = notice(10)
            await app.raw(new_update(app.tg.msgs[10]))
        assert 10 not in app.tg.msgs
        await app.db.close()

    run(go())


# ============================================================================================== live changes
def test_a_new_post_by_someone_else_is_added_a_moment_later(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        m = foreign(app.tg, "from the Telegram app", markup=markup([url_btn("Open", "https://example.org")]))
        await app.raw(new_update(m))
        assert await app.db.find_by_message(1, m.id) is None  # not yet: the wait lets the bot finish its own business
        await sy.flush_events()
        p = await app.db.find_by_message(1, m.id)
        assert p.source == "adopted" and p.text == "from the Telegram app" and p.buttons == [[{"t": "Open", "u": "https://example.org"}]]
        await app.db.close()

    run(go())


def test_the_bots_own_posts_are_not_looked_at(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        mine = app.tg._new("sent by the bot", None, None)  # out=True
        await app.raw(new_update(mine))
        assert not sy._pending()
        await app.db.close()

    run(go())


def test_nothing_is_queued_while_a_long_job_runs_or_when_the_sync_is_off(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        m = foreign(app.tg, "x")
        async with app.ctx.lock:
            await app.raw(new_update(m))
            await app.raw(edit_update(app.tg.msgs[1]))
        assert not sy._pending()
        off = await make_app(tmp_path / "off", posts=three(), sync_interval_minutes=0)
        m2 = foreign(off.tg, "y")
        await off.raw(new_update(m2))
        await off.raw(edit_update(off.tg.msgs[1]))
        await off.raw(delete_update(1, [2]))
        assert not off.ctx.syncer._pending() and len(await off.db.sent_posts(1)) == 3
        await app.db.close()
        await off.db.close()

    (tmp_path / "off").mkdir()
    run(go())


def test_an_edit_made_by_an_admin_is_recorded(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        m = app.tg.msgs[3]
        m.message, m.entities = "third, with a correction", [types.MessageEntityBold(0, 5)]
        await app.raw(edit_update(m))
        await sy.flush_events()
        p = await app.db.find_by_message(1, 3)
        assert p.text == "third, with a correction" and p.entities == [{"k": "bold", "o": 0, "l": 5}]
        assert sy.last_reports[1].edited == [3]
        await app.db.close()

    run(go())


def test_the_echo_of_an_edit_made_through_the_bot_changes_nothing(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        sy.rt_grace = 60  # the bot saved the post a moment ago
        pid = (await app.db.find_by_message(1, 1)).id
        await app.db.update_post(pid, text="the owner's new text")  # updated_at = now
        app.tg.msgs[1].message = "the owner's new text"
        await app.raw(edit_update(app.tg.msgs[1]))
        sy.max_tries = 1
        await sy.flush_events()
        assert (await app.db.find_by_message(1, 1)).text == "the owner's new text"
        assert sy.last_reports[1].edited == []
        await app.db.close()

    run(go())


def test_a_post_deleted_by_an_admin_leaves_my_posts_at_once(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        m = foreign(app.tg, "will be deleted before the wait is over")
        await app.raw(new_update(m))
        await app.raw(edit_update(app.tg.msgs[2]))
        del app.tg.msgs[2]
        await app.raw(delete_update(1, [2, m.id]))
        assert await app.db.find_by_message(1, 2) is None  # no flush needed
        assert sy._news == {1: set()} and sy._edits == {1: set()}  # and nothing is left to look at for them
        await sy.flush_events()
        assert await app.db.find_by_message(1, m.id) is None
        assert sorted(p.message_id for p in await app.db.sent_posts(1)) == [1, 3]
        await app.db.close()

    run(go())


def test_deleted_posts_of_other_channels_are_ignored(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        await app.raw(delete_update(77, [1, 2, 3]))
        assert len(await app.db.sent_posts(1)) == 3
        await app.db.close()

    run(go())


def test_a_message_that_is_still_too_fresh_is_looked_at_again(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        sy.rt_grace, sy.max_tries = 0.15, 500
        m = foreign(app.tg, "just posted", age=0)
        await app.raw(new_update(m))
        await asyncio.wait_for(sy.flush_events(), 5)
        p = await app.db.find_by_message(1, m.id)
        assert p is not None and p.source == "adopted"
        assert not sy._pending() and sy._tries == {}
        await app.db.close()

    run(go())


def test_a_message_that_never_gets_old_enough_is_given_up(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        sy.rt_grace, sy.max_tries = 3600, 2
        m = foreign(app.tg, "from the future", age=0)
        await app.raw(new_update(m))
        await asyncio.wait_for(sy.flush_events(), 5)
        assert await app.db.find_by_message(1, m.id) is None and not sy._pending() and sy._tries == {}
        await app.db.close()

    run(go())


def test_the_queue_waits_for_a_long_job_to_finish(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        m = foreign(app.tg, "posted before the long job started")
        await app.raw(new_update(m))
        async with app.ctx.lock:
            task = asyncio.create_task(sy.flush_events())
            await asyncio.sleep(0.15)
            assert not task.done() and await app.db.find_by_message(1, m.id) is None
        await asyncio.wait_for(task, 5)
        assert (await app.db.find_by_message(1, m.id)).source == "adopted"
        await app.db.close()

    run(go())


def test_a_burst_of_changes_becomes_one_look_at_the_whole_channel(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        sy.max_ids = 2
        for i in (1, 2, 3):
            app.tg.msgs[i].message = f"edited {i}"
            await app.raw(edit_update(app.tg.msgs[i]))
        await sy.flush_events()
        rep = sy.last_reports[1]
        assert rep.checked == 3 and sorted(rep.edited) == [1, 2, 3]  # compared as a whole, not id by id
        await app.db.close()

    run(go())


# ===================================================================================================== the timer
def test_the_timed_check_looks_at_every_channel_and_gives_way_to_long_jobs(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        app.tg.msgs[2].message = "edited meanwhile"
        async with app.ctx.lock:
            assert await sy.run_all() == []  # a long job is running: nothing is read
        assert (await app.db.find_by_message(1, 2)).text == "second"
        reports = await sy.run_all()
        assert [r.channel.id for r in reports] == [1] and reports[0].edited == [2]
        assert sy.last_reports[1] is reports[0]
        await app.db.close()

    run(go())


def test_one_channel_that_fails_does_not_stop_the_others(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        broken = app.tg.add_channel(FakeChannelClient({1: old_msg(1, "x")}, username="broken", cid=2, title="Broken"))
        await app.db.save_channel(2, 6, "Broken", "broken", 1)
        await app.db.create_post(channel_id=2, message_id=1, status="sent", source="bot", created_by=1, text="x",
                                 created_at=OLD, updated_at=OLD, sent_at=OLD)
        broken.fail["GetMessagesRequest"] = RuntimeError("the network dropped")
        app.tg.msgs[3].message = "edited"
        reports = await app.ctx.syncer.run_all()
        by_id = {r.channel.id: r for r in reports}
        assert by_id[1].edited == [3] and by_id[1].error is None
        assert by_id[2].error and "RuntimeError" in by_id[2].error
        await app.db.close()

    run(go())


def test_the_timer_starts_and_stops_with_the_bot(tmp_path):
    async def go():
        off = await make_app(tmp_path / "off", posts=three(), sync_interval_minutes=0)
        off.ctx.syncer.start()
        assert off.ctx.syncer._tasks == [] and off.ctx.syncer.enabled is False

        app = await make_app(tmp_path, posts=three())
        sy = app.ctx.syncer
        app.tg.msgs[1].message = "edited"
        sy.start()
        assert len(sy._tasks) == 2
        for _ in range(100):
            if 1 in sy.last_reports:
                break
            await asyncio.sleep(0.05)
        assert sy.last_reports[1].edited == [1]
        await sy.stop()
        assert sy._tasks == []
        await app.db.close()
        await off.db.close()

    (tmp_path / "off").mkdir()
    run(go())


def test_the_syncer_is_part_of_the_context_the_bot_builds(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        assert isinstance(app.ctx.syncer, Syncer)
        await app.db.close()

    run(go())


# ============================================================================================ the /sync command
def test_sync_is_for_the_owner_only(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        app.uid = 555
        await app.text("/sync")
        assert "private" in norm(app.out.last_text).lower() or "owner" in norm(app.out.last_text).lower()
        assert app.ctx.syncer.last_reports == {}
        await app.db.close()

    run(go())


def test_sync_needs_a_channel_and_understands_only_its_options(tmp_path):
    async def go():
        bare = App(tmp_path, edit_delay=0)
        await bare.start()
        await bare.text("/sync")
        assert "No channels registered" in norm(bare.out.last_text)
        await bare.db.close()

        app = await make_app(tmp_path / "b", posts=three())
        await app.text("/sync --bogus")
        assert "I don't know “--bogus”" in norm(app.out.last_text) and "Usage: /sync" in norm(app.out.last_text)
        await app.text("/sync --channel @nowhere")
        assert "isn't registered" in norm(app.out.last_text)
        await app.db.close()

    (tmp_path / "b").mkdir()
    run(go())


def test_sync_reports_what_it_found_in_every_channel(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        await app.text("/sync")  # the first look
        first = norm(app.out.last_text)
        assert "Sync finished" in first and "Anime Channel" in first and "First look" in first
        assert "3 saved post(s) compared" in first

        app.tg.msgs[2].message = "second, edited"
        del app.tg.msgs[3]
        app.tg._pts += 2
        new = foreign(app.tg, "from the app")
        app.tg.msgs[new.id + 1] = notice(new.id + 1)
        app.tg._top = new.id + 1
        app.tg._pts += 1
        app.tg.msgs[2].reply_markup = None  # and the channel shows no keyboard for post 2
        await app.text("/sync")
        text = norm(app.out.last_text)
        assert "1 deleted in the channel" in text and "message 3" in text
        assert "1 edited in the channel" in text and "message 2" in text
        assert "1 new post(s) made outside the bot" in text and f"message {new.id}" in text
        assert "1 service message(s) deleted" in text
        assert "show no buttons in the channel (message 2)" in text and "Check buttons" in text
        assert not app.ctx.lock.locked()
        await app.text("/sync")
        assert "Everything matches." in norm(app.out.last_text) or "show no buttons" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_sync_looks_at_one_channel_only_when_asked(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        app.tg.add_channel(FakeChannelClient({}, username="movies", cid=2, title="Movies"))
        await app.db.save_channel(2, 6, "Movies", "movies", 1)
        await app.text("/sync --channel @movies")
        text = norm(app.out.last_text)
        assert "Movies" in text and "Anime Channel" not in text
        assert set(app.ctx.syncer.last_reports) == {2}
        await app.db.close()

    run(go())


def test_sync_holds_the_long_job_lock_and_refuses_a_second_run(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        gate = asyncio.Event()
        real_get = app.tg.get_messages
        seen = []

        async def slow(peer, ids=None):
            seen.append(app.ctx.lock.locked())
            await gate.wait()
            return await real_get(peer, ids=ids)

        app.tg.get_messages = slow
        first = asyncio.create_task(app.text("/sync"))
        await asyncio.sleep(0.1)
        await app.text("/sync")
        assert "still running" in norm(app.out.last_text)
        gate.set()
        await asyncio.wait_for(first, 5)
        assert seen and all(seen) and not app.ctx.lock.locked()
        await app.db.close()

    run(go())


def test_sync_reports_a_channel_it_cannot_read(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        app.tg.fail["GetFullChannelRequest"] = errors.ChannelPrivateError(request=None)
        await app.text("/sync")
        text = norm(app.out.last_text)
        assert "Anime Channel" in text and "ChannelPrivateError" in text
        assert len(await app.db.sent_posts(1)) == 3
        await app.db.close()

    run(go())


# ================================================================================== the rest of the bot
def test_forgetting_a_post_in_the_bot_keeps_the_sync_from_bringing_it_back(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        p = await app.db.find_by_message(1, 2)
        await app.press(f"dly:{p.id}:b")
        assert await app.db.find_by_message(1, 2) is None and (1, 2) in app.ctx.sync_ignore
        assert 2 in app.tg.msgs  # still in the channel
        await app.text("/sync")  # the first look
        del app.tg.msgs[2]
        assert (1, 2) in app.ctx.sync_ignore
        await app.db.close()

    run(go())


def test_a_post_the_sync_saved_first_does_not_stop_the_bot_from_publishing(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        draft = await app.db.create_post(channel_id=1, status="draft", text="my new post", created_by=1)
        sent = app.tg._new("my new post", None, None)  # what Telegram will answer the publish with
        await app.db.adopt_message(1, sent.id, {"text": "picked up by the sync a moment earlier"})

        async def send_message(peer, message="", **kw):
            return sent

        app.tg.send_message = send_message
        await app.press(f"pby:{draft.id}")
        p = await app.db.find_by_message(1, sent.id)
        assert p.id == draft.id and p.source == "bot" and p.text == "my new post"
        await app.db.close()

    run(go())


def test_the_panel_says_where_an_adopted_post_came_from(tmp_path):
    from app.ui import panel_text

    async def go():
        app = await make_app(tmp_path, posts=three())
        p, _ = await app.db.adopt_message(1, 40, {"text": "made in the app"})
        ch = await app.db.get_channel(1)
        assert "not made with this bot" in panel_text(p, ch)
        await app.db.close()

    run(go())


def test_photos_of_new_posts_survive_the_trip_to_my_posts(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three())
        m = foreign(app.tg, "a captioned photo", media=photo_media(41))
        await app.raw(new_update(m))
        await app.ctx.syncer.flush_events()
        p = await app.db.find_by_message(1, m.id)
        assert p.media_kind == "photo" and p.media_file_id.startswith("tl:")
        await app.db.close()

    run(go())


def test_utcnow_is_what_the_fresh_checks_compare_with():
    assert (utcnow() - NOW).total_seconds() < 3600  # sanity for the helpers above
