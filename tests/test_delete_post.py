"""Deleting one published post from My posts: the bot first, the userbot when Telegram refuses, honest messages."""
import asyncio
import datetime

from app.db import utcnow

from .fakes import make_msg
from .harness import App, norm
from .test_repost_flow import fill, userbot_setup


async def make_app(tmp_path, **cfg):
    app = App(tmp_path, **cfg)
    await app.start()
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    fill(app.tg, {5: make_msg(5, "old post"), 6: make_msg(6, "newer post")})
    post = await app.db.create_post(channel_id=1, message_id=5, status="sent", source="bot", text="old post", entities=[], buttons=[])
    return app, post.id


def test_the_bot_deletes_a_post_it_is_allowed_to(tmp_path):
    async def run():
        app, pid = await make_app(tmp_path)
        await app.press(f"dl:{pid}")
        text = norm(app.out.last_text)
        # the screen says what each button does; it does not talk about a helper account or ask about one
        assert "Delete this post" in text and "removes it, with its data, from My posts" in text
        assert "userbot" not in text.lower() and "if you set one up" not in text
        assert [t for row in app.out.last_buttons() for t in row] == ["🗑 Delete from channel", "🧹 Only forget it in the bot", "↩️ Back"]
        await app.press(f"dly:{pid}:a")
        assert "Done - deleted in the channel and removed from My posts" in norm(app.out.last_text)
        assert 5 not in app.tg.msgs and 6 in app.tg.msgs
        assert await app.db.get_post(pid) is None
        await app.db.close()

    asyncio.run(run())


def test_a_post_too_old_for_the_bot_stays_registered_and_the_owner_is_told_why(tmp_path):
    async def run():
        app, pid = await make_app(tmp_path)
        app.tg.undeletable = {5}
        await app.press(f"dly:{pid}:a")
        text = norm(app.out.last_text)
        assert "Telegram did not delete that message" in text and "/userbot" in text and "Only forget it in the bot" in text
        assert "still in My posts" in text
        assert 5 in app.tg.msgs and await app.db.get_post(pid) is not None  # nothing is forgotten while it still exists
        # after deleting by hand, "only forget it" removes just the bot's record
        await app.press(f"dly:{pid}:b")
        assert await app.db.get_post(pid) is None and 5 in app.tg.msgs
        await app.db.close()

    asyncio.run(run())


def test_the_userbot_deletes_what_the_bot_may_not(tmp_path):
    async def run():
        app, pid = await make_app(tmp_path)
        ub, holder = userbot_setup(app)
        app.tg.undeletable = {5}
        shown = len(app.out.log)
        await app.press(f"dly:{pid}:a")
        # one answer, no question on the way: the userbot simply took over where the bot was refused
        assert len(app.out.log) - shown == 1
        assert "Done - deleted in the channel and removed from My posts" in norm(app.out.last_text)
        assert "the userbot did" in norm(app.out.last_text) and app.out.last_buttons() == [["📚 My posts"]]
        assert 5 not in app.tg.msgs and 6 in app.tg.msgs and await app.db.get_post(pid) is None
        assert holder["client"].deleted == [[5]]
        assert app.tg.admins == {} and all(v["revoked"] for v in app.tg.invites.values())  # cleaned up again
        await app.db.close()

    asyncio.run(run())


def test_a_userbot_that_cannot_get_the_right_is_explained(tmp_path):
    async def run():
        app, pid = await make_app(tmp_path)
        userbot_setup(app, bot_rights=dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=False))
        app.tg.undeletable = {5}
        await app.press(f"dly:{pid}:a")
        text = norm(app.out.last_text)
        assert "Telegram did not delete that message" in text and "Add new admins" in text
        assert 5 in app.tg.msgs and await app.db.get_post(pid) is not None
        await app.db.close()

    asyncio.run(run())


def test_discarding_a_draft_touches_nothing_in_telegram(tmp_path):
    async def run():
        app, _ = await make_app(tmp_path)
        d = await app.db.create_post(channel_id=1, status="draft", source="bot", text="a draft", entities=[], buttons=[])
        await app.press(f"dl:{d.id}")
        assert "Discard this draft" in norm(app.out.last_text)
        await app.press(f"dly:{d.id}:b")
        assert await app.db.get_post(d.id) is None
        assert app.tg.requests == [] and app.tg.deleted == []
        await app.db.close()

    asyncio.run(run())


def test_deleting_from_the_channel_removes_the_post_and_what_hangs_on_it(tmp_path):
    """The saved post, its timed deletion and a "forgotten on purpose" mark all go with the channel message."""
    async def run():
        app, pid = await make_app(tmp_path)
        other = await app.db.create_post(channel_id=1, message_id=6, status="sent", source="bot", text="newer post", entities=[], buttons=[])
        await app.db.schedule_deletes(
            [
                dict(channel_id=1, message_id=5, delete_at=utcnow() + datetime.timedelta(days=1), group_id="g", created_by=1),
                dict(channel_id=1, message_id=6, delete_at=utcnow() + datetime.timedelta(days=1), group_id="g", created_by=1),
            ]
        )
        await app.db.ignore_post(1, 5)
        await app.press(f"dly:{pid}:a")
        assert await app.db.get_post(pid) is None and 5 not in app.tg.msgs
        left = await app.db.pending_deletes()
        assert [r.message_id for r in left] == [6]  # only the timer of the message that is still there
        assert 5 not in await app.db.ignored_ids(1)
        assert (await app.db.get_post(other.id)) is not None  # the other post is untouched
        await app.db.close()

    asyncio.run(run())


def test_only_forgetting_a_post_leaves_it_in_the_channel_and_keeps_the_sync_away_from_it(tmp_path):
    async def run():
        app, pid = await make_app(tmp_path)
        await app.press(f"dly:{pid}:b")
        assert await app.db.get_post(pid) is None and 5 in app.tg.msgs
        assert 5 in await app.db.ignored_ids(1) and (1, 5) in app.ctx.sync_ignore
        assert app.tg.deleted == [] and "Done" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())
