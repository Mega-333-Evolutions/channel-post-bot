"""Only the Share URL: the autorename bot asks for the link of a file in the Database Channel; nothing is published."""
from app.dbpublish import LinkNotice, parse_link_notice, parse_notice, parse_delete_notice

from .test_dbpublish import FILE_LINK, FakeReader, FakeUserbot, file_post, handle, make_app, notice, run


def ask(post=None, ref="l1"):
    lines = ["Get the share URL"]
    if post is not None:
        lines.append(f"Post: {post}")
    if ref:
        lines.append(f"Ref: {ref}")
    return "\n".join(lines)


def test_the_message_is_read_and_is_nothing_else():
    assert parse_link_notice(ask(7)) == LinkNotice(post_id=7, ref="l1")
    assert parse_link_notice("GET the Share URL\npost : x").post_id is None
    assert parse_link_notice("please get the share url\nPost: 7") is None and parse_link_notice(None) is None
    assert parse_notice(ask(7)) is None and parse_delete_notice(ask(7)) is None and parse_link_notice(notice(post=3)) is None


def test_the_link_is_returned_and_nothing_is_posted(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        post = file_post(app)
        ev = await handle(app, ask(post.id))
        assert ev.replies[0] == "📥 Message received - looking for the Share URL…\nRef: l1"
        assert ev.replies[1] == f"🔗 Share URL ready\nURL: {FILE_LINK}\nRef: l1"
        assert not app.tg.sent and not app.tg.requests
        await app.db.close()

    run(go())


def test_the_userbot_reads_it_when_the_bot_cannot_see_the_button(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        hidden = file_post(app, buttons=False)
        seen = file_post(app)
        reader = FakeReader(app.clock, {hidden.id: seen})
        app.ctx.userbot = FakeUserbot(reader)
        ev = await handle(app, ask(hidden.id))
        assert ev.replies[-1].startswith("🔗 Share URL ready") and "read through the userbot" in ev.replies[-1]
        await app.db.close()

    run(go())


def test_failures_are_reported(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        none = await handle(app, ask(None), mid=2)
        assert "no “Post:” line" in none.replies[-1] and none.replies[-1].startswith("❌ Share URL failed: ")
        bare = file_post(app, buttons=False)
        no_button = await handle(app, ask(bare.id), mid=3)
        assert "No 🔁 Share URL button" in no_button.replies[-1] and "Ref: l1" in no_button.replies[-1]
        await app.db.close()

    run(go())
