import asyncio

from app.tgutil import button_url
from .harness import App, norm

SEEDED = [[{"t": "Download Episodes 01 to 20", "u": "https://t.me/oldbot?start=aaa"}]]


async def make_app(tmp_path):
    app = App(tmp_path)
    await app.start()
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    return app


def sent_markup(app, i=-1):
    return app.tg.sent[i][3]["buttons"]


def test_suggestion_flow_end_to_end(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.db.create_post(channel_id=1, message_id=10, status="sent", source="bot", text="Click the button below to download 👇", entities=[], buttons=SEEDED)

        await app.text("/new")
        assert "Suggestion" in app.out.last_text and "Download Episodes 21 to 40" in app.out.last_text
        await app.press(app.out.callback_data("Use suggestion"))
        assert "Send the link" in app.out.last_text
        await app.text("https://t.me/newbot?start=zzz")
        assert "Ready" in app.out.last_text and "Publish" in str(app.out.last_buttons())

        pid = int(app.out.callback_data("Publish").split(":")[1])
        await app.press(f"pb:{pid}")
        assert "Publish this post" in app.out.last_text
        await app.press(f"pby:{pid}")
        assert "Published" in app.out.last_text
        kind, peer, msg, kw = app.tg.sent[-1]
        assert msg == "Click the button below to download 👇"
        btn = sent_markup(app).rows[0].buttons[0]
        assert btn.text == "Download Episodes 21 to 40" and button_url(btn) == "https://t.me/newbot?start=zzz"
        p = await app.db.get_post(pid)
        assert p.status == "sent" and p.message_id == 100

        # the next suggestion moves on again
        await app.text("/new")
        assert "Download Episodes 41 to 60" in app.out.last_text
        await app.db.close()

    asyncio.run(run())


def test_customize_new_post_with_buttons_and_edit_live(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/new")  # nothing to suggest yet -> asks for the post
        assert "Send the post now" in app.out.last_text
        await app.text("Hello *world*")  # stored literally (no markdown parsing)
        assert "buttons" in app.out.last_text.lower()
        await app.text("Get it - https://t.me/bot?start=1 | Join - t.me/animech\nMirror - https://example.com/x")
        pid = int(app.out.callback_data("Publish").split(":")[1])
        post = await app.db.get_post(pid)
        assert post.text == "Hello *world*" and [len(r) for r in post.buttons] == [2, 1]
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        assert app.tg.sent[-1][2] == "Hello *world*"

        # edit the live post: change a button's text, then its link
        await app.press(f"bs:{pid}:0:0")
        assert "Get it" in app.out.last_text
        await app.press(f"bt:{pid}:0:0")
        await app.text("Download now")
        await app.press(f"bu:{pid}:0:0")
        await app.text("https://t.me/goku?start=1")
        last_edit = app.tg.edits[-1][2]
        assert last_edit.message == "Hello *world*"
        b = last_edit.reply_markup.rows[0].buttons[0]
        assert b.text == "Download now" and button_url(b) == "https://t.me/goku?start=1"

        # reorder: move the first button right, then down into its own row
        await app.press(f"bm:{pid}:0:0:r")
        rows = (await app.db.get_post(pid)).buttons
        assert [b["t"] for b in rows[0]] == ["Join", "Download now"]
        await app.press(f"bm:{pid}:0:1:d")
        rows = (await app.db.get_post(pid)).buttons
        assert [[b["t"] for b in r] for r in rows] == [["Join"], ["Mirror", "Download now"]]

        # delete a button
        await app.press(f"bd:{pid}:0:0")
        assert [[b["t"] for b in r] for r in (await app.db.get_post(pid)).buttons] == [["Mirror", "Download now"]]
        live = app.tg.edits[-1][2].reply_markup
        assert [b.text for b in live.rows[0].buttons] == ["Mirror", "Download now"]

        # edit text of the live post keeps the buttons
        await app.press(f"et:{pid}")
        await app.text("New text")
        req = app.tg.edits[-1][2]
        assert req.message == "New text" and len(req.reply_markup.rows[0].buttons) == 2

        # delete from the channel
        await app.press(f"dly:{pid}:a")
        assert app.tg.deleted == [[100]] and await app.db.get_post(pid) is None
        await app.db.close()

    asyncio.run(run())


def test_bad_input_and_permissions(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/new")
        await app.text("A post")
        await app.text("this has no link")
        assert "No link found" in norm(app.out.last_text)
        await app.text("/skip")
        pid = int(app.out.callback_data("Publish").split(":")[1])
        await app.press(f"ba:{pid}")
        await app.text("Only text")  # asks for the link next
        assert "send the link" in app.out.last_text.lower()
        await app.text("not a link")
        assert "doesn't look like a link" in norm(app.out.last_text)
        await app.text("https://t.me/bot?start=q")
        assert (await app.db.get_post(pid)).buttons[0][0]["u"] == "https://t.me/bot?start=q"

        # a non-owner is rejected everywhere
        app.uid = 999
        await app.text("/replace @a @b")
        assert "private" in app.out.last_text.lower()
        await app.db.close()

    asyncio.run(run())


def test_drafts_list_and_discard(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/new")
        await app.text("draft one")
        await app.text("/skip")
        await app.text("/posts")
        await app.press("pc:d:0")
        assert "Drafts" in app.out.last_text and "draft one" in str(app.out.last_buttons())
        pid = int(app.out.callback_data("draft one").split(":")[1])
        await app.press(f"dly:{pid}:b")
        assert await app.db.get_post(pid) is None
        await app.db.close()

    asyncio.run(run())
