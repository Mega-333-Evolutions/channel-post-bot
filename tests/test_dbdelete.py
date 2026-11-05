"""Deleting an automatically published post: the autorename bot's "Delete the published post" message in the communication
chat -> the post goes from its channel and from My posts, and the answer says so (or says why not)."""
import asyncio
import datetime
from types import SimpleNamespace

import pytest

from app.db import utcnow
from app.dbpublish import DeleteNotice, parse_delete_notice, parse_notice

from .fakes import FakeChannelClient, rpc
from .test_dbpublish import BLACK, BLACKM, COMM, DBM, FILE, Comm, file_post, handle, make_app, notice, notice_handler, run


def removal(*, channel=BLACKM, message=None, file=FILE, ref="d1"):
    """What the autorename bot writes when its "Delete post" button is confirmed."""
    lines = ["Delete the published post"]
    if channel is not None:
        lines.append(f"Channel: {channel}")
    if message is not None:
        lines.append(f"Message: {message}")
    if file:
        lines.append(f"File: {file}")
    if ref:
        lines.append(f"Ref: {ref}")
    return "\n".join(lines)


async def make_series(tmp_path, **cfg):
    """The app, and the channel of the series as a simulated channel of its own: posts land in it and can be deleted from it."""
    app = await make_app(tmp_path, **cfg)
    black = FakeChannelClient(cid=BLACK, title="Black Clover English", username="blackclover")
    app.tg.add_channel(black)

    async def send(peer, message="", **kw):
        return SimpleNamespace(id=black._new(message, None, None).id)

    app.tg.send_message = send
    return app, black


async def publish(app, black, *, ref="p1", mid=900):
    """Publish an episode the way the autorename bot asks for it (event id 900: no clash with the deletions); the message id of the new post."""
    db_post = file_post(app)
    ev = await handle(app, notice(post=db_post.id, ref=ref), mid=mid)
    assert ev.replies[-1].startswith("✅ Post published successfully"), ev.replies
    (row,) = await app.db.latest_sent(BLACK)
    return row.message_id


class FakeDeleter:
    """What the userbot hands over for deleting: the posts it deletes are gone from the simulated channel."""

    def __init__(self, black, *, fail=None):
        self.black, self.fail, self.asked, self.closed = black, fail, [], 0

    async def delete(self, ids):
        self.asked.append(list(ids))
        if self.fail is not None:
            raise self.fail
        for i in ids:
            self.black.msgs.pop(i, None)

    async def close(self):
        self.closed += 1


class FakeUserbot:
    def __init__(self, deleter=None, *, enabled=True, ready=True, last_error=""):
        self.deleter, self.enabled, self.ready, self.last_error = deleter, enabled, ready, last_error

    async def fallback_for(self, ch):
        return (self.deleter.delete, self.deleter) if self.deleter is not None else (None, None)


# ------------------------------------------------------------------------------------------------ the message
def test_the_delete_message_is_read_line_by_line():
    n = parse_delete_notice(removal(message=57))
    assert n == DeleteNotice(channel=BLACKM, channel_raw=str(BLACKM), message_id=57, file=FILE, ref="d1")
    again = parse_delete_notice("  DELETE   the Published Post\nchannel : -100123\nmessage:5\nREF: x")
    assert (again.channel, again.message_id, again.file, again.ref) == (-100123, 5, "", "x")


def test_it_must_begin_with_the_words_and_is_not_an_upload_notice():
    assert parse_delete_notice("Please delete the published post\nChannel: -100123\nMessage: 5") is None
    assert parse_delete_notice("Message received\nDelete the published post") is None
    assert parse_delete_notice(None) is None and parse_delete_notice("") is None
    assert parse_delete_notice(notice(post=3)) is None  # an upload notice is no deletion ...
    assert parse_notice(removal(message=3)) is None  # ... and a deletion is no upload notice


@pytest.mark.parametrize("message", ["", "abc", "12 34", "-5", "1" * 13])
def test_a_message_id_that_is_no_number_is_none(message):
    assert parse_delete_notice(f"Delete the published post\nChannel: {BLACKM}\nMessage: {message}").message_id is None


# ----------------------------------------------------------------------------------------- the post names its id
def test_the_published_reply_names_the_message_id_of_the_post(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        ev = await handle(app, notice(post=file_post(app).id, ref="p1"))
        (row,) = await app.db.latest_sent(BLACK)
        done = ev.replies[-1]
        assert f"\nMessage: {row.message_id}\n" in done and f"Post: https://t.me/blackclover/{row.message_id}\n" in done
        assert row.message_id in black.msgs
        await app.db.close()

    run(go())


# ---------------------------------------------------------------------------------------------- the deletion
def test_the_post_is_deleted_in_the_channel_and_in_my_posts(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        assert msg_id in black.msgs and (await app.db.find_by_message(BLACK, msg_id)) is not None
        ev = await handle(app, removal(message=msg_id), mid=2)
        assert ev.replies[0] == "📥 Message received - deleting the post…\nRef: d1"
        assert len(ev.replies) == 2
        done = ev.replies[1]
        assert done.startswith("🗑 Post deleted successfully")
        for part in ("Channel: Black Clover English", f"Message: {msg_id}", f"File: {FILE}", "Ref: d1"):
            assert part in done, done
        assert ev.kw == {"parse_mode": None, "link_preview": False}
        assert msg_id not in black.msgs and black.delete_requests == [[msg_id]]
        assert await app.db.find_by_message(BLACK, msg_id) is None
        assert (await app.db.list_posts(BLACK, None, 0, 10))[1] == 0  # not in My posts any more
        assert app.tg.delete_requests == [] and app.tg.msgs  # the Database Channel was not touched
        await app.db.close()

    run(go())


def test_only_the_named_post_goes(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        first = await publish(app, black, ref="p1", mid=1)
        second_file = file_post(app, "Black Clover - S2 E06.mkv")
        await handle(app, notice(post=second_file.id, episode="06", file="Black Clover - S2 E06.mkv", ref="p2"), mid=2)
        rows = await app.db.latest_sent(BLACK)
        second = next(r.message_id for r in rows if r.message_id != first)
        await handle(app, removal(message=first), mid=3)
        assert first not in black.msgs and second in black.msgs
        assert [r.message_id for r in await app.db.latest_sent(BLACK)] == [second]
        await app.db.close()

    run(go())


def test_what_hangs_on_the_post_goes_with_it(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        await app.db.schedule_deletes(
            [dict(channel_id=BLACK, message_id=msg_id, delete_at=utcnow() + datetime.timedelta(days=1), group_id="g", created_by=1)]
        )
        await app.db.ignore_post(BLACK, msg_id)
        await handle(app, removal(message=msg_id), mid=2)
        assert await app.db.pending_deletes() == [] and msg_id not in await app.db.ignored_ids(BLACK)
        await app.db.close()

    run(go())


def test_a_post_that_was_deleted_by_hand_is_removed_from_my_posts(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        del black.msgs[msg_id]  # deleted in Telegram: My posts does not know yet
        ev = await handle(app, removal(message=msg_id), mid=2)
        assert ev.replies[-1].startswith("🗑 Post deleted successfully")
        assert await app.db.find_by_message(BLACK, msg_id) is None
        await app.db.close()

    run(go())


def test_a_post_that_is_gone_everywhere_counts_as_deleted(tmp_path):
    """Pressing the button twice, or deleting the post in My posts first, must not end in a failure."""
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        await handle(app, removal(message=msg_id, ref="a"), mid=2)
        again = await handle(app, removal(message=msg_id, ref="b"), mid=3)
        assert again.replies[-1].startswith("🗑 Post deleted successfully")
        assert "ℹ️ The post was already gone from the channel and from My posts." in again.replies[-1]
        assert "Ref: b" in again.replies[-1] and black.delete_requests == [[msg_id]]  # nothing was asked of Telegram twice
        await app.db.close()

    run(go())


def test_a_message_that_is_not_in_my_posts_but_still_in_the_channel_is_left_alone(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        by_hand = black._new("somebody's own post", None, None).id
        ev = await handle(app, removal(message=by_hand))
        failed = ev.replies[-1]
        assert failed.startswith("❌ Deletion failed: ") and f"Message {by_hand} of “Black Clover English” is not one of the posts" in failed
        assert "so I did not delete it" in failed and f"File: {FILE}" in failed and "Ref: d1" in failed
        assert by_hand in black.msgs and black.delete_requests == []
        await app.db.close()

    run(go())


@pytest.mark.parametrize("fields", [dict(created_by=1, source="bot"), dict(created_by=0, source="adopted")])
def test_a_post_that_was_not_published_automatically_is_left_alone(tmp_path, fields):
    """A post somebody made with the bot (or one it adopted) is deleted from My posts, never by a message in the chat."""
    async def go():
        app, black = await make_series(tmp_path)
        mid = black._new("a post made by hand", None, None).id
        row = await app.db.create_post(
            channel_id=BLACK, message_id=mid, status="sent", text="a post made by hand", entities=[], buttons=[], **fields
        )
        ev = await handle(app, removal(message=mid))
        assert "was not published automatically, so I did not delete it" in ev.replies[-1] and ev.replies[-1].startswith("❌ Deletion failed")
        assert mid in black.msgs and black.delete_requests == [] and (await app.db.get_post(row.id)) is not None
        await app.db.close()

    run(go())


def test_a_post_the_bot_may_not_delete_stays_and_the_reason_is_given(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        black.undeletable = {msg_id}  # like a post older than 48 hours
        ev = await handle(app, removal(message=msg_id))
        failed = ev.replies[-1]
        assert failed.startswith("❌ Deletion failed: Telegram did not delete the post in “Black Clover English”.")
        assert "/userbot" in failed and "still in My posts" in failed and "Ref: d1" in failed
        assert msg_id in black.msgs and (await app.db.find_by_message(BLACK, msg_id)) is not None
        await app.db.close()

    run(go())


def test_telegrams_refusal_is_named(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        black.delete_error = rpc("ChatAdminRequiredError")
        ev = await handle(app, removal(message=msg_id))
        assert "Telegram refused the bot (ChatAdminRequiredError)" in ev.replies[-1]
        assert msg_id in black.msgs and (await app.db.find_by_message(BLACK, msg_id)) is not None
        await app.db.close()

    run(go())


def test_the_userbot_deletes_what_the_bot_may_not(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        black.undeletable = {msg_id}
        deleter = FakeDeleter(black)
        app.ctx.userbot = FakeUserbot(deleter)
        ev = await handle(app, removal(message=msg_id))
        done = ev.replies[-1]
        assert done.startswith("🗑 Post deleted successfully") and "ℹ️ The bot was not allowed to delete it, so the userbot did." in done
        assert deleter.asked == [[msg_id]] and deleter.closed == 1  # the deleter is always closed again
        assert msg_id not in black.msgs and (await app.db.find_by_message(BLACK, msg_id)) is None
        await app.db.close()

    run(go())


def test_the_userbot_is_not_used_when_the_bot_can_delete(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        deleter = FakeDeleter(black)
        app.ctx.userbot = FakeUserbot(deleter)
        ev = await handle(app, removal(message=msg_id))
        assert ev.replies[-1].startswith("🗑 Post deleted successfully") and "userbot" not in ev.replies[-1]
        assert deleter.asked == [] and deleter.closed == 1
        await app.db.close()

    run(go())


def test_a_userbot_that_cannot_delete_is_explained(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        black.undeletable = {msg_id}
        deleter = FakeDeleter(black, fail=RuntimeError("no right"))
        app.ctx.userbot = FakeUserbot(deleter)
        ev = await handle(app, removal(message=msg_id))
        assert "The userbot could not help: no right" in ev.replies[-1] and ev.replies[-1].startswith("❌ Deletion failed")
        assert deleter.closed == 1 and msg_id in black.msgs and (await app.db.find_by_message(BLACK, msg_id)) is not None
        await app.db.close()

    run(go())


def test_a_userbot_that_is_not_connected_is_named(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        black.undeletable = {msg_id}
        app.ctx.userbot = FakeUserbot(None, ready=False, last_error="session expired")
        ev = await handle(app, removal(message=msg_id))
        assert "The userbot is not connected: session expired" in ev.replies[-1]
        await app.db.close()

    run(go())


# ------------------------------------------------------------------------------------ what cannot be deleted
@pytest.mark.parametrize(
    "text, complaint",
    [
        (removal(channel=None, message=5), "does not say which channel the post is in: it has no “Channel:” line"),
        (removal(channel="Black Clover", message=5), "names “Black Clover” as the channel, which is not a channel id"),
        (removal(channel=DBM, message=5), "is the Database Channel itself - this bot only deletes the posts it made in the channel of the series"),
        (removal(channel=-1009999999999, message=5), "-1009999999999 is not connected to this bot"),
        (removal(message=None), "does not say which post to delete: it has no “Message:” line"),
    ],
)
def test_a_message_that_cannot_work_is_refused_with_its_reason_and_nothing_is_touched(tmp_path, text, complaint):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        ev = await handle(app, text, mid=2)
        assert ev.replies[-1].startswith("❌ Deletion failed: ") and complaint in ev.replies[-1] and "Ref: d1" in ev.replies[-1]
        assert msg_id in black.msgs and black.delete_requests == [] and app.tg.delete_requests == []
        assert (await app.db.find_by_message(BLACK, msg_id)) is not None
        await app.db.close()

    run(go())


def test_a_channel_that_is_not_connected_any_more_cannot_be_deleted_in(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        await app.db.set_channel_active(BLACK, False)
        ev = await handle(app, removal(message=msg_id))
        assert f"The channel {BLACKM} is not connected to this bot" in ev.replies[-1] and "so I cannot delete there" in ev.replies[-1]
        assert msg_id in black.msgs
        await app.db.close()

    run(go())


def test_when_telegram_cannot_be_asked_the_reason_is_reported(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        black.fail["GetMessagesRequest"] = rpc("ChannelPrivateError")  # the bot cannot read the channel at all
        ev = await handle(app, removal(message=msg_id, ref="a"), mid=2)
        assert ev.replies[-1].startswith("❌ Deletion failed: Telegram refused to look at the post in “Black Clover English”")
        assert (await app.db.find_by_message(BLACK, msg_id)) is not None
        await app.db.delete_post((await app.db.find_by_message(BLACK, msg_id)).id)  # (not in My posts any more: the other path)
        ev = await handle(app, removal(message=msg_id, ref="b"), mid=3)
        assert ev.replies[-1].startswith("❌ Deletion failed: I could not look at the post in “Black Clover English”")
        await app.db.close()

    run(go())


# ---------------------------------------------------------------------------------------- the conversation
def test_something_unexpected_still_gets_an_answer(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)

        async def boom(notice):
            raise RuntimeError("kaput")

        app.ctx.publisher.delete = boom
        ev = await handle(app, removal(message=5))
        assert ev.replies[-1].startswith("❌ Deletion failed: something went wrong inside the bot (RuntimeError: kaput)")
        assert "Ref: d1" in ev.replies[-1]
        await app.db.close()

    run(go())


def test_the_same_message_is_answered_once(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        first = await handle(app, removal(message=msg_id), mid=7)
        again = await handle(app, removal(message=msg_id), mid=7)  # delivered twice
        assert len(first.replies) == 2 and again.replies == []
        await app.db.close()

    run(go())


def test_a_deletion_waits_for_the_post_that_is_being_made(tmp_path):
    """One thing at a time: the answer "received" comes at once, the deletion when the lock is free."""
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        pub = app.ctx.publisher
        await pub.lock.acquire()
        ev = Comm(removal(message=msg_id), mid=5)
        task = asyncio.ensure_future(pub.handle(ev))
        await asyncio.sleep(0.5)  # (the database answers from another thread: that takes real time)
        assert len(ev.replies) == 1 and ev.replies[0].startswith("📥 Message received") and msg_id in black.msgs
        pub.lock.release()
        await task
        assert len(ev.replies) == 2 and ev.replies[1].startswith("🗑 Post deleted successfully") and msg_id not in black.msgs
        await app.db.close()

    run(go())


def test_the_listener_deletes_only_for_the_autorename_bot_in_the_communication_chat(tmp_path):
    async def go():
        app, black = await make_series(tmp_path)
        msg_id = await publish(app, black)
        fn = notice_handler(app)
        elsewhere = Comm(removal(message=msg_id), mid=2, chat=-1000000000555)
        stranger = Comm(removal(message=msg_id), mid=3, sender=12345)
        await fn(elsewhere)
        await fn(stranger)
        assert elsewhere.replies == [] and stranger.replies == [] and msg_id in black.msgs
        good = Comm(removal(message=msg_id), mid=4)
        await fn(good)
        assert len(good.replies) == 2 and good.replies[1].startswith("🗑 Post deleted successfully") and msg_id not in black.msgs
        assert good.chat_id == COMM
        await app.db.close()

    run(go())
