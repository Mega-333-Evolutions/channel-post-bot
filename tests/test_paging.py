"""My posts: "Prev" is always there, and on the first page it goes to the last page."""
import asyncio

from .harness import App


async def make_app(tmp_path, n_posts, uid_owner=1):
    app = App(tmp_path, owner=uid_owner)
    await app.start()
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    for i in range(n_posts):
        await app.db.create_post(
            channel_id=1, message_id=100 + i, status="sent", source="bot", text=f"Post number {i}", entities=[], buttons=[]
        )
    return app


def nav_row(app):
    """Labels of the navigation row (the one with the page counter), or None."""
    for row in app.out.log[-1][2] or []:
        labels = [b.text for b in row]
        if any("/" in t and t.replace("/", "").isdigit() for t in labels):
            return labels
    return None


def data_of(app, label_part):
    return app.out.callback_data(label_part)


def test_prev_on_the_first_page_goes_to_the_last_page(tmp_path):
    async def run():
        app = await make_app(tmp_path, 83)  # 8 per page -> 11 pages, like "1/11" in the screenshot
        await app.text("/posts")
        await app.press(app.out.callback_data("Anime Channel"))
        assert nav_row(app) == ["⬅️ Prev", "1/11", "Next ➡️"]  # Prev is shown on page 1 as well

        await app.press(app.out.callback_data("Prev"))
        assert nav_row(app) == ["⬅️ Prev", "11/11"]  # the last page has no Next
        assert "83 post(s)" in app.out.last_text
        # the last page holds the 3 remaining posts
        posts = [r for r in app.out.log[-1][2] if r and r[0].text.startswith("✅")]
        assert len(posts) == 3

        await app.press(app.out.callback_data("Prev"))
        assert nav_row(app) == ["⬅️ Prev", "10/11", "Next ➡️"]
        await app.db.close()

    asyncio.run(run())


def test_next_walks_forward_and_stops_at_the_end(tmp_path):
    async def run():
        app = await make_app(tmp_path, 20)  # 3 pages
        await app.text("/posts")
        await app.press(app.out.callback_data("Anime Channel"))
        assert nav_row(app) == ["⬅️ Prev", "1/3", "Next ➡️"]
        await app.press(app.out.callback_data("Next"))
        assert nav_row(app) == ["⬅️ Prev", "2/3", "Next ➡️"]
        await app.press(app.out.callback_data("Next"))
        assert nav_row(app) == ["⬅️ Prev", "3/3"]
        await app.press(app.out.callback_data("Prev"))
        await app.press(app.out.callback_data("Prev"))
        assert nav_row(app) == ["⬅️ Prev", "1/3", "Next ➡️"]
        await app.press(app.out.callback_data("Prev"))  # and around again
        assert nav_row(app) == ["⬅️ Prev", "3/3"]
        await app.db.close()

    asyncio.run(run())


def test_a_single_page_has_nothing_to_turn(tmp_path):
    async def run():
        app = await make_app(tmp_path, 5)
        await app.text("/posts")
        await app.press(app.out.callback_data("Anime Channel"))
        assert nav_row(app) is None
        assert "5 post(s)" in app.out.last_text
        await app.db.close()

    asyncio.run(run())


def test_drafts_list_wraps_too(tmp_path):
    async def run():
        app = await make_app(tmp_path, 0)
        for i in range(10):
            await app.db.create_post(channel_id=1, status="draft", source="bot", text=f"draft {i}", entities=[], buttons=[])
        await app.press("pc:d:0")
        assert nav_row(app) == ["⬅️ Prev", "1/2", "Next ➡️"]
        await app.press(app.out.callback_data("Prev"))
        assert nav_row(app) == ["⬅️ Prev", "2/2"]
        await app.db.close()

    asyncio.run(run())


def test_check_buttons_row_is_for_the_owner_and_for_channel_lists_only(tmp_path):
    async def run():
        app = await make_app(tmp_path, 3)
        await app.press("pc:1:0")
        assert "Check buttons" in str(app.out.last_buttons())
        await app.press("pc:d:0")  # drafts belong to no channel: nothing to check
        assert "Check buttons" not in str(app.out.last_buttons())
        await app.db.close()

        # an allowed non-owner does not get the repair tool
        app2 = App(tmp_path, owner=1, admins=frozenset({7}))
        await app2.start()
        await app2.db.save_channel(1, 5, "Anime Channel", "animech", 1)
        app2.uid = 7
        await app2.press("pc:1:0")
        assert "Check buttons" not in str(app2.out.last_buttons())
        await app2.db.close()

    asyncio.run(run())
