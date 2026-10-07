"""/broadcast (and its spelling /boardcast): one message to every channel, optionally deleted again after a time."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telethon import errors, types

from app.db import Channel, as_utc, utcnow
from app.expiry import MAX_ATTEMPTS, Expirer, human, parse_duration
from app.tgutil import button_url

from .fakes import FakeChannelClient, photo_media
from .harness import App, norm


def run(coro):
    return asyncio.run(coro)


def posted(app):
    """Messages the bot sent to channels (not to people)."""
    return [e for e in app.tg.sent if isinstance(e[1], types.InputPeerChannel)]


async def make_app(tmp_path, channels=2, **cfg):
    """Channel 1 "Anime Channel" is the app's own fake; channel 2 "Movie Channel" sits next to it. Posts the bot sends
    to a channel really appear in it, so deleting them really deletes."""
    app = App(tmp_path, edit_delay=0, **cfg)
    await app.start()
    app.tg.username, app.tg.title = "animech", "Anime Channel"
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    if channels > 1:
        app.tg.add_channel(FakeChannelClient({}, username="moviech", cid=2, title="Movie Channel"))
        await app.db.save_channel(2, 6, "Movie Channel", "moviech", 1)

    async def send_message(peer, message="", **kw):
        if not isinstance(peer, types.InputPeerChannel):  # a person (the preview)
            app.tg.sent.append(("text", peer, message, kw))
            return SimpleNamespace(id=next(app.tg.ids))
        app.tg.sent.append(("text", peer, message, kw))
        return app.tg.registry[peer.channel_id]._new(message, kw.get("formatting_entities"), kw.get("buttons"))

    async def send_file(peer, file, **kw):
        app.tg.sent.append(("file", peer, file, kw))
        if not isinstance(peer, types.InputPeerChannel):
            return SimpleNamespace(id=next(app.tg.ids))
        return app.tg.registry[peer.channel_id]._new(kw.get("caption"), kw.get("formatting_entities"), kw.get("buttons"), media=file)

    app.tg.send_message, app.tg.send_file = send_message, send_file
    app.ctx.expirer = Expirer(app.ctx)
    return app


async def broadcast(app, command="/broadcast 1h", text="Hello everyone", buttons=None, message=None):
    await app.text(command)
    await app.text(text, message=message)
    if buttons is None:
        await app.press(app.out.callback_data("No buttons"))
    else:
        await app.text(buttons)
    await app.press(app.out.callback_data("Send to all channels"))


# ----------------------------------------------------------------------------------- the time
def test_times_are_minutes_hours_days():
    assert parse_duration("50m") == 3000
    assert parse_duration("1h") == 3600
    assert parse_duration("5d") == 5 * 86400
    assert parse_duration("1d12h") == 129600
    assert parse_duration("90 min") == 5400
    assert parse_duration("2 Hours") == 7200
    assert parse_duration("1w") == 604800
    for bad in ("", "soon", "10", "h", "0m", "30s", "5x", "400d", "1h 5"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_times_are_spelled_out():
    assert human(3000) == "50 minutes"
    assert human(3600) == "1 hour"
    assert human(5400) == "1 hour 30 minutes"
    assert human(129600) == "1 day 12 hours"
    assert human(5 * 86400) == "5 days"
    assert human(90061) == "1 day 1 hour"
    assert human(20) == "under a minute"


# ----------------------------------------------------------------------------------- sending
def test_a_timed_broadcast_goes_to_every_channel_and_is_remembered(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        before = utcnow()
        await app.text("/broadcast 1h")
        assert "2 channel(s)" in norm(app.out.last_text) and "1 hour" in norm(app.out.last_text)
        await app.text("Hello everyone")
        assert "Buttons?" in norm(app.out.last_text)
        await app.press(app.out.callback_data("No buttons"))
        shown = norm(app.out.last_text)
        assert "Send it to 2 channel(s)" in shown and "Anime Channel" in shown and "Movie Channel" in shown
        assert "Deleted again 1 hour after sending" in shown
        # the preview went to the person, nothing to the channels yet
        assert [e[2] for e in app.tg.sent if not isinstance(e[1], types.InputPeerChannel)] == ["Hello everyone"]
        assert posted(app) == []

        await app.press(app.out.callback_data("Send to all channels"))
        report = norm(app.out.last_text)
        assert "Broadcast sent to 2 of 2" in report and "✅ Anime Channel" in report and "✅ Movie Channel" in report
        assert "deleted from these channels and from My posts in 1 hour" in report and "UTC" in report
        assert [e[2] for e in posted(app)] == ["Hello everyone", "Hello everyone"]
        assert app.tg.registry[1].msgs[1].message == "Hello everyone" and app.tg.registry[2].msgs[1].message == "Hello everyone"

        for cid in (1, 2):  # in My posts, as published posts of the bot
            (p,) = await app.db.sent_posts(cid)
            assert (p.message_id, p.text, p.source, p.created_by) == (1, "Hello everyone", "bot", 1)
        rows = await app.db.pending_deletes()
        assert sorted((r.channel_id, r.message_id) for r in rows) == [(1, 1), (2, 1)]
        for r in rows:
            assert as_utc(r.delete_at) - before >= timedelta(hours=1) and as_utc(r.delete_at) - utcnow() <= timedelta(hours=1)
        assert len({r.group_id for r in rows}) == 1
        assert app.ctx.expirer._next is not None
        await app.db.close()

    run(go())


def test_without_a_time_the_message_stays_and_the_other_spelling_works(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await broadcast(app, "/boardcast")
        report = norm(app.out.last_text)
        assert "Broadcast sent to 2 of 2" in report and "stays in the channels" in report
        assert await app.db.pending_deletes() == []
        assert app.ctx.expirer._next is None
        assert len(posted(app)) == 2
        await app.db.close()

    run(go())


def test_buttons_and_formatting(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        bold = types.MessageEntityBold(0, 5)
        msg = SimpleNamespace(raw_text="Hello all", entities=[bold], media=None, grouped_id=None, forward=None)
        await app.text("/broadcast 2d")
        await app.text("Hello all", message=msg)
        await app.text("Join - https://t.me/animech | Site - https://example.com/x\nMore - t.me/moviech")
        assert "Send it to 2 channel(s)" in norm(app.out.last_text) and "2 days" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Send to all channels"))
        first = posted(app)[0]
        assert first[2] == "Hello all"
        assert [type(e).__name__ for e in first[3]["formatting_entities"]] == ["MessageEntityBold"]
        rows = first[3]["buttons"].rows
        assert [[b.text for b in r.buttons] for r in rows] == [["Join", "Site"], ["More"]]
        assert button_url(rows[1].buttons[0]) == "https://t.me/moviech"
        (p,) = await app.db.sent_posts(2)
        assert p.buttons == [[{"t": "Join", "u": "https://t.me/animech"}, {"t": "Site", "u": "https://example.com/x"}], [{"t": "More", "u": "https://t.me/moviech"}]]
        assert p.entities == [{"k": "bold", "o": 0, "l": 5}]
        await app.db.close()

    run(go())


def test_a_photo_with_a_caption(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        msg = SimpleNamespace(raw_text="New poster", entities=[], media=photo_media(5), grouped_id=None, forward=None)
        await broadcast(app, "/broadcast 30m", "New poster", message=msg)
        files = [e for e in posted(app) if e[0] == "file"]
        assert len(files) == 2 and files[0][3]["caption"] == "New poster"
        got = app.tg.registry[2].msgs[1]
        assert got.message == "New poster" and isinstance(got.media, types.MessageMediaPhoto)
        (p,) = await app.db.sent_posts(2)
        assert p.media_kind == "photo" and p.media_file_id.startswith("tl:") and p.text == "New poster"
        await app.db.close()

    run(go())


def test_a_channel_that_refuses_does_not_stop_the_others(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        real = app.tg.send_message

        async def picky(peer, message="", **kw):
            if getattr(peer, "channel_id", None) == 2:
                raise errors.ChatWriteForbiddenError(request=None)
            return await real(peer, message, **kw)

        app.tg.send_message = picky
        await broadcast(app, "/broadcast 1h")
        report = norm(app.out.last_text)
        assert "sent to 1 of 2" in report and "✅ Anime Channel" in report
        assert "❌ Movie Channel" in report and "ChatWriteForbiddenError" in report
        assert [r.channel_id for r in await app.db.pending_deletes()] == [1]  # only what was sent expires
        assert await app.db.sent_posts(2) == []
        await app.db.close()

    run(go())


def test_only_the_owner_and_only_good_input(tmp_path):
    async def go():
        app = await make_app(tmp_path, admins=frozenset({7}))
        app.uid = 7
        await app.text("/broadcast 1h")
        assert "private" in app.out.last_text and 7 not in app.ctx.state
        app.uid = 1
        await app.text("/broadcast soon")
        assert "can't read" in norm(app.out.last_text) and 1 not in app.ctx.state
        await app.text("/broadcast 20s")
        assert "don't know the unit" in norm(app.out.last_text)
        await app.text("/broadcast 400d")
        assert "1 year" in app.out.last_text
        # an admin cannot press the owner's buttons either
        await app.text("/broadcast 1h")
        await app.text("Hi")
        await app.press(app.out.callback_data("No buttons"))
        confirm = app.out.callback_data("Send to all channels")
        app.uid = 7
        await app.press(confirm)
        assert "Only the bot owner" in app.out.last_text and posted(app) == []
        await app.db.close()

    run(go())


def test_nothing_is_sent_without_a_yes_and_a_button_works_once(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/broadcast")
        await app.text("Hi")
        await app.press(app.out.callback_data("No buttons"))
        await app.press(app.out.callback_data("Cancel"))
        assert "Cancelled" in app.out.last_text and posted(app) == []

        await app.text("/broadcast")
        await app.text("Hi")
        await app.press(app.out.callback_data("No buttons"))
        send = app.out.callback_data("Send to all channels")
        await app.press(send)
        await app.press(send)
        assert "expired" in app.out.last_text
        assert len(posted(app)) == 2
        await app.db.close()

    run(go())


def test_wrong_buttons_and_a_busy_bot_are_explained(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/broadcast 1h")
        await app.text("Hi")
        await app.text("no link here")
        assert "Try again" in norm(app.out.last_text)
        await app.text("Go - https://t.me/x")
        send = app.out.callback_data("Send to all channels")
        await app.ctx.lock.acquire()
        await app.press(send)
        assert "still running" in app.out.last_text and posted(app) == []
        app.ctx.lock.release()
        await app.press(send)
        assert len(posted(app)) == 2
        await app.db.close()

    run(go())


def test_no_channels_no_broadcast(tmp_path):
    async def go():
        app = App(tmp_path, edit_delay=0)
        await app.start()
        await app.text("/broadcast 1h")
        assert "No channels yet" in app.out.last_text
        await app.db.close()

    run(go())


# ------------------------------------------------------------------------------------ deleting
async def timed_broadcast(app, command="/broadcast 1h"):
    await broadcast(app, command)
    return utcnow()


def test_when_the_time_is_up_the_message_goes_from_the_channels_and_my_posts(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        now = await timed_broadcast(app)
        ex = app.ctx.expirer

        early = await ex.sweep(now=now + timedelta(minutes=59))
        assert early["deleted"] == 0 and 1 in app.tg.registry[1].msgs
        assert len(await app.db.sent_posts(1)) == 1

        done = await ex.sweep(now=now + timedelta(hours=1, minutes=1))
        assert done["deleted"] == 2 and done["retry"] == 0 and done["failed"] == 0
        assert app.tg.registry[1].msgs == {} and app.tg.registry[2].msgs == {}
        assert await app.db.sent_posts(1) == [] and await app.db.sent_posts(2) == []
        assert await app.db.pending_deletes() == []
        assert ex._next is None  # nothing left: the loop sleeps until something new is scheduled
        await app.db.close()

    run(go())


def test_other_posts_are_not_touched_and_a_message_deleted_by_hand_is_fine(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        keep = await app.db.create_post(channel_id=1, message_id=50, status="sent", source="bot", text="keep me", entities=[], buttons=[])
        now = await timed_broadcast(app)
        app.tg.registry[2].msgs.pop(1)  # an admin deleted the broadcast in channel 2 already
        done = await app.ctx.expirer.sweep(now=now + timedelta(hours=2))
        assert done["deleted"] == 2
        assert (await app.db.get_post(keep.id)) is not None
        assert [p.message_id for p in await app.db.sent_posts(1)] == [50]
        assert await app.db.sent_posts(2) == []
        await app.db.close()

    run(go())


def test_a_restart_loses_nothing(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        now = await timed_broadcast(app, "/broadcast 5d")
        due = now + timedelta(days=5)
        fresh = Expirer(app.ctx)  # a new process: it knows nothing but the database
        assert fresh._next is None
        await fresh.load()
        assert abs((fresh._next - due).total_seconds()) < 30
        # it was off for a week: everything that came due is deleted when it is back
        await fresh.sweep(now=now + timedelta(days=7))
        assert app.tg.registry[1].msgs == {} and app.tg.registry[2].msgs == {}
        assert await app.db.sent_posts(1) == []
        await app.db.close()

    run(go())


def test_the_background_loop_wakes_when_something_is_scheduled_and_when_it_is_due(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        ex = app.ctx.expirer
        ex.start()
        await asyncio.sleep(0.05)  # idle: nothing to do, nothing polled
        msg = app.tg._new("soon gone", None, None)
        when = utcnow() + timedelta(seconds=0.4)
        await app.db.schedule_deletes([dict(channel_id=1, message_id=msg.id, delete_at=when, group_id="g", created_by=1)])
        ex.notify(when)
        for _ in range(60):
            if msg.id not in app.tg.msgs:
                break
            await asyncio.sleep(0.05)
        assert msg.id not in app.tg.msgs
        await ex.stop()
        await app.db.close()

    run(go())


def test_something_already_overdue_is_deleted_at_start(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        msg = app.tg._new("overdue", None, None)
        await app.db.schedule_deletes([dict(channel_id=1, message_id=msg.id, delete_at=utcnow() - timedelta(hours=3), group_id="g", created_by=1)])
        ex = Expirer(app.ctx)
        ex.start()
        for _ in range(60):
            if msg.id not in app.tg.msgs:
                break
            await asyncio.sleep(0.05)
        assert msg.id not in app.tg.msgs
        await ex.stop()
        await app.db.close()

    run(go())


def test_a_running_job_makes_it_wait(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        now = await timed_broadcast(app)
        ex = app.ctx.expirer
        await app.ctx.lock.acquire()
        later = now + timedelta(hours=2)
        out = await ex.sweep(now=later)
        assert out["waiting"] and out["deleted"] == 0 and 1 in app.tg.registry[1].msgs
        assert 50 <= (ex._next - later).total_seconds() <= 70
        app.ctx.lock.release()
        out = await ex.sweep(now=later)
        assert out["deleted"] == 2
        await app.db.close()

    run(go())


def test_what_the_bot_cannot_delete_is_retried_then_reported(tmp_path):
    async def go():
        app = await make_app(tmp_path, channels=1)
        now = await timed_broadcast(app)
        app.tg.undeletable = {1}  # Telegram refuses (an old post)
        ex = app.ctx.expirer
        t = now + timedelta(hours=2)
        for attempt in range(1, MAX_ATTEMPTS):
            out = await ex.sweep(now=t)
            assert out["retry"] == 1 and out["failed"] == 0, attempt
            (row,) = await app.db.pending_deletes()
            assert row.attempts == attempt and as_utc(row.delete_at) > t
            t = as_utc(row.delete_at) + timedelta(seconds=1)
        dms_before = len([e for e in app.tg.sent if not isinstance(e[1], types.InputPeerChannel)])
        out = await ex.sweep(now=t)
        assert out["failed"] == 1
        assert await app.db.pending_deletes() == []
        (row,) = await app.db.pending_deletes(status="failed")
        assert row.attempts == MAX_ATTEMPTS and row.last_error
        # the person who sent it is told; the post stays in My posts because it is still in the channel
        told = [e for e in app.tg.sent if not isinstance(e[1], types.InputPeerChannel)][dms_before:]
        assert told and "could not delete the expired broadcast" in told[-1][2]
        assert len(await app.db.sent_posts(1)) == 1
        assert ex._next is None
        await app.db.close()

    run(go())


def test_the_userbot_deletes_what_the_bot_may_not(tmp_path):
    async def go():
        app = await make_app(tmp_path, channels=1)
        now = await timed_broadcast(app)
        app.tg.undeletable = {1}
        calls, closed = [], []

        class StubUserbot:
            enabled = True

            async def fallback_for(self, ch):
                async def fn(ids):
                    calls.append(list(ids))
                    for i in ids:
                        app.tg.msgs.pop(i, None)

                async def close():
                    closed.append(1)

                return fn, SimpleNamespace(close=close)

        app.ctx.userbot = StubUserbot()
        out = await app.ctx.expirer.sweep(now=now + timedelta(hours=2))
        assert out["deleted"] == 1 and calls == [[1]] and closed == [1]
        assert app.tg.msgs == {} and await app.db.sent_posts(1) == []
        await app.db.close()

    run(go())


def test_a_channel_taken_out_of_the_bot_is_reported_not_retried_forever(tmp_path):
    async def go():
        app = await make_app(tmp_path, channels=1)
        now = await timed_broadcast(app)
        await app.db.forget_posts(1, [p.message_id for p in await app.db.sent_posts(1)])  # a real database refuses to drop a channel its posts still point at
        async with app.db.Session() as s:
            await s.delete(await s.get(Channel, 1))
            await s.commit()
        out = await app.ctx.expirer.sweep(now=now + timedelta(hours=2))
        assert out["failed"] == 1 and out["deleted"] == 0
        assert await app.db.pending_deletes() == []
        await app.db.close()

    run(go())
