"""Taking a channel out of the bot: the bot asks whether the channel's saved posts (its data in My posts) are deleted as
well - Yes removes the channel and the data, No removes only the channel and keeps the data."""
import asyncio

from app.db import utcnow
from app.sync_engine import sync_channel

from .harness import App, norm
from .test_sync import old_msg, opts, world


def run(coro):
    return asyncio.run(coro)


async def make_app(tmp_path, **cfg):
    """Two channels: "Anime Channel" (id 1) has three published posts and a draft, "Other Channel" (id 2) has two posts;
    channel 1 also has what the sync keeps (where it stopped, a post it must forget) and a timed deletion."""
    app = App(tmp_path, edit_delay=0, **cfg)
    await app.start()
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    await app.db.save_channel(2, 6, "Other Channel", "otherch", 1)
    for cid, n in ((1, 3), (2, 2)):
        for i in range(1, n + 1):
            await app.db.create_post(
                channel_id=cid, message_id=i, status="sent", source="adopted", text=f"post {cid}/{i}", entities=[], buttons=[]
            )
    await app.db.create_post(channel_id=1, status="draft", source="bot", text="a draft", entities=[], buttons=[])
    for cid in (1, 2):
        await app.db.save_sync(cid, last_top=3, last_pts=3)
        await app.db.start_history(cid, 3)
        await app.db.ignore_post(cid, 9)
        app.ctx.sync_ignore.add((cid, 9))
    await app.db.schedule_deletes(
        [dict(channel_id=1, message_id=2, delete_at=utcnow(), group_id="g", created_by=1)]
    )
    return app


async def tap_channel(app, label_part="1"):
    await app.text("/channels")
    await app.press(app.out.callback_exact(label_part))


# ============================================================================================ the question
def test_the_owner_is_asked_whether_the_saved_posts_go_too(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await tap_channel(app)
        text = norm(app.out.last_text)
        assert "Take Anime Channel out of the bot" in text
        assert "Do you also want to delete its saved posts" in text and "4 post(s)" in text  # three published + a draft
        assert "Nothing is deleted in the channel itself" in text
        assert app.out.last_buttons() == [["🗑 Yes, delete them too", "📦 No, keep them"], ["🔙 Back"]]
        # the question alone changes nothing
        assert [c.id for c in await app.db.list_channels()] == [1, 2] and await app.db.count_channel_posts(1) == 4
        await app.press(app.out.callback_exact("🔙 Back"))
        assert "Your channels" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_a_channel_without_saved_posts_has_nothing_to_ask(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.db.save_channel(3, 7, "Empty Channel", "emptych", 1)
        await app.text("/channels")
        await app.press(app.out.callback_exact("2"))  # Anime, Empty, Other: the second one
        text = norm(app.out.last_text)
        assert "Take Empty Channel out of the bot?" in text and "no saved posts" in text
        assert app.out.last_buttons() == [["✅ Yes, remove it", "🔙 Back"]]
        await app.press(app.out.callback_exact("✅ Yes, remove it"))
        assert [c.id for c in await app.db.list_channels()] == [1, 2]
        await app.db.close()

    run(go())


# ================================================================================================ yes
def test_yes_removes_the_channel_and_deletes_its_saved_posts(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await tap_channel(app)
        await app.press(app.out.callback_data("Yes, delete them too"))
        text = norm(app.out.last_text)
        assert "Anime Channel was removed from the bot" in text and "4 saved post(s) were deleted from My posts" in text
        assert "Nothing was deleted in the channel" in text
        assert [c.id for c in await app.db.list_channels()] == [2]
        row = await app.db.get_channel(1)
        assert row is not None and not row.active  # switched off, not erased: a repost or shift that names it still finds it
        assert await app.db.count_channel_posts(1) == 0 and await app.db.sent_posts(1) == []
        _, total = await app.db.list_posts(1, "draft", 0, 10)
        assert total == 0
        # what the sync kept about the channel goes too, so it starts from scratch when it is added again
        assert await app.db.get_sync(1) is None and await app.db.get_history(1) is None
        assert await app.db.ignored_ids(1) == set() and not any(k[0] == 1 for k in app.ctx.sync_ignore)
        # ... and nothing of the other channel is touched
        assert await app.db.count_channel_posts(2) == 2
        assert await app.db.get_sync(2) is not None and await app.db.get_history(2) is not None
        assert await app.db.ignored_ids(2) == {9} and (2, 9) in app.ctx.sync_ignore
        # a timed deletion is a promise about a message that is still in the channel: it stays
        assert [r.message_id for r in await app.db.pending_deletes()] == [2]
        await app.db.close()

    run(go())


def test_nothing_is_deleted_in_telegram_when_the_data_goes(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        before = len(app.tg.requests)
        await tap_channel(app)
        await app.press(app.out.callback_data("Yes, delete them too"))
        assert app.tg.deleted == [] and len(app.tg.requests) == before
        await app.db.close()

    run(go())


def test_the_posts_are_not_deleted_while_a_look_at_the_channels_is_running(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        lock = app.ctx.syncer.exclusive()
        await lock.acquire()  # a look at the channels is in progress: it could save posts again
        task = asyncio.create_task(app.press("chy:1:1"))
        await asyncio.sleep(0.2)
        assert not task.done() and await app.db.count_channel_posts(1) == 4
        lock.release()
        await task
        assert await app.db.count_channel_posts(1) == 0
        await app.db.close()

    run(go())


# =================================================================================================== no
def test_no_removes_only_the_channel_and_keeps_its_saved_posts(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await tap_channel(app)
        await app.press(app.out.callback_data("No, keep them"))
        text = norm(app.out.last_text)
        assert "Anime Channel was removed from the bot" in text and "saved posts are kept" in text
        assert [c.id for c in await app.db.list_channels()] == [2]
        assert await app.db.count_channel_posts(1) == 4 and await app.db.get_sync(1) is not None
        assert await app.db.ignored_ids(1) == {9} and (1, 9) in app.ctx.sync_ignore
        # added again: everything is there again
        await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
        assert [c.id for c in await app.db.list_channels()] == [1, 2] and await app.db.count_channel_posts(1) == 4
        await app.db.close()

    run(go())


def test_a_button_from_the_old_question_still_removes_only_the_channel(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.press("chy:1")  # what the earlier version of the bot put on its button
        assert [c.id for c in await app.db.list_channels()] == [2]
        assert await app.db.count_channel_posts(1) == 4
        assert "saved posts are kept" in norm(app.out.last_text)
        await app.db.close()

    run(go())


# ============================================================================================ who may do it
def test_only_an_owner_can_remove_a_channel_or_delete_its_data(tmp_path):
    async def go():
        app = await make_app(tmp_path, owner=7, admins=frozenset({1}))
        app.uid = 1  # an admin, not an owner
        for data in ("chd:1", "chy:1:1", "chy:1:0", "chy:1"):
            await app.press(data)
        assert [c.id for c in await app.db.list_channels()] == [1, 2] and await app.db.count_channel_posts(1) == 4
        await app.db.close()

    run(go())


# ========================================================================================= coming back
def test_a_channel_that_comes_back_after_its_data_was_deleted_is_read_in_again(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {i: old_msg(i, f"older post {i}") for i in range(1, 6)}, save=False)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.first_look and rep.imported == 5
        assert await db.delete_channel_data(1) == 5
        assert await db.sent_posts(1) == [] and await db.get_sync(1) is None and await db.get_history(1) is None
        rep = await sync_channel(chan, db, ch, opts())  # as if it had never been looked at
        assert rep.first_look and rep.imported == 5 and rep.history_done
        assert len(await db.sent_posts(1)) == 5
        await db.close()

    run(go())
