"""/autopost (a series of episode posts, then the real links) and "Mass replace links" in a post's panel."""
import asyncio

import pytest
from telethon import errors

from app.autopost import (
    PLACEHOLDER_LINK,
    button_label,
    parse_autopost_args,
    plan_ranges,
    post_text,
    ranges_summary,
)
from app.tgutil import button_url

from .harness import App, norm


def run(coro):
    return asyncio.run(coro)


async def make_app(tmp_path, **cfg):
    app = App(tmp_path, edit_delay=0, **cfg)
    await app.start()
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    return app


def sent_markup(entry):
    return entry[3]["buttons"]


def link_edits(app):
    """(message id, new link) for every keyboard-only edit the bot made."""
    out = []
    for kind, mid, req in app.tg.edits:
        if kind == "raw" and req.message is None and req.reply_markup is not None:
            out.append((mid, button_url(req.reply_markup.rows[0].buttons[0])))
    return out


# ------------------------------------------------------------------------------- the plan
def test_the_ranges_follow_the_interval():
    assert plan_ranges(114, 20) == [(1, 20), (21, 40), (41, 60), (61, 80), (81, 100), (101, 114)]
    assert plan_ranges(100, 20) == [(1, 20), (21, 40), (41, 60), (61, 80), (81, 100)]
    assert plan_ranges(20, 20) == [(1, 20)]


def test_a_short_end_is_added_to_the_post_before_it():
    assert plan_ranges(102, 20)[-2:] == [(61, 80), (81, 102)]  # 2 left over: less than half of 20
    assert plan_ranges(110, 20)[-1] == (81, 110)  # exactly half of 20: still added
    assert plan_ranges(111, 20)[-2:] == [(81, 100), (101, 111)]  # 11 is more than half: its own post
    assert plan_ranges(114, 20)[-1] == (101, 114)  # 14 is more than half
    assert plan_ranges(22, 15) == [(1, 22)]  # half of 15 is 7.5: 7 left over is added ...
    assert plan_ranges(23, 15) == [(1, 15), (16, 23)]  # ... 8 is not


def test_a_total_below_the_interval_is_one_post_and_small_intervals_work():
    assert plan_ranges(14, 20) == [(1, 14)]
    assert plan_ranges(3, 20) == [(1, 3)]
    assert plan_ranges(3, 1) == [(1, 1), (2, 2), (3, 3)]
    assert plan_ranges(5, 2) == [(1, 2), (3, 5)]  # one left over = half of 2: added to the post before it
    with pytest.raises(ValueError):
        plan_ranges(0, 20)


def test_every_episode_is_in_exactly_one_post():
    for total in range(1, 130):
        for interval in (1, 2, 3, 7, 10, 15, 20, 25):
            ranges = plan_ranges(total, interval)
            flat = [n for a, b in ranges for n in range(a, b + 1)]
            assert flat == list(range(1, total + 1)), (total, interval)
            assert all(b - a + 1 <= interval + interval // 2 for a, b in ranges)


def test_texts_are_the_ones_asked_for():
    assert post_text(1, 20) == "Episodes 01 to 20\n\nClick the button below to download 👇"
    assert post_text(101, 114) == "Episodes 101 to 114\n\nClick the button below to download 👇"
    assert button_label(21, 40) == "Download Episodes 21 to 40"
    assert button_label(5, 5) == "Download Episode 05"
    assert ranges_summary(plan_ranges(114, 20)) == "01–20 · 21–40 · 41–60 · 61–80 · 81–100 · 101–114"
    assert ranges_summary([(i, i) for i in range(1, 40)]).endswith("… · 38 · 39")


def test_the_arguments():
    assert parse_autopost_args("114 20") == (114, 20)
    assert parse_autopost_args("  12,  5 ") == (12, 5)
    for bad in ("", "114", "114 20 3", "a b", "0 20", "20 0", "-5 3", "1.5 2", "999999999 20"):
        with pytest.raises(ValueError):
            parse_autopost_args(bad)


# ------------------------------------------------------------------------------- the command
def test_the_whole_autopost_flow(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopost 114 20")
        shown = norm(app.out.last_text)
        assert "6 post(s)" in shown and "01–20 · 21–40 · 41–60 · 61–80 · 81–100 · 101–114" in shown
        assert "Which channel" in shown and "Anime Channel" in str(app.out.last_buttons())

        await app.press(app.out.callback_data("Anime Channel"))
        posted = [e for e in app.tg.sent if e[0] == "text" and e[1].channel_id == 1]
        assert len(posted) == 6
        assert [e[2] for e in posted] == [post_text(a, b) for a, b in plan_ranges(114, 20)]
        for (a, b), entry in zip(plan_ranges(114, 20), posted):
            row = sent_markup(entry).rows[0].buttons
            assert len(row) == 1 and row[0].text == f"Download Episodes {a:02d} to {b:02d}"
            assert button_url(row[0]) == PLACEHOLDER_LINK == "https://t.me/English"
            assert entry[3]["link_preview"] is False

        # all six are in My posts, as published posts of this channel
        posts = await app.db.sent_posts(1)
        assert [p.message_id for p in posts] == [100, 101, 102, 103, 104, 105]
        assert posts[0].source == "bot" and posts[0].buttons == [[{"t": "Download Episodes 01 to 20", "u": PLACEHOLDER_LINK}]]

        # the first link is asked for at once, with a cancel button
        ask = norm(app.out.last_text)
        assert "1 of 6" in ask and "Download Episodes 01 to 20" in ask and PLACEHOLDER_LINK in ask
        assert "Cancel" in str(app.out.last_buttons()) and "Skip" in str(app.out.last_buttons())

        links = [f"https://t.me/mybot?start=part{i}" for i in range(1, 7)]
        for i, url in enumerate(links):
            await app.text(url)
            if i < 5:
                nxt = norm(app.out.last_text)
                assert f"{i + 2} of 6" in nxt and "now opens" in nxt and url in nxt
                a, b = plan_ranges(114, 20)[i + 1]
                assert f"Download Episodes {a:02d} to {b:02d}" in nxt
        assert "All done" in app.out.last_text and "6 link(s) replaced" in app.out.last_text

        # each answer changed only the keyboard of its own message, at once
        assert link_edits(app) == [(100 + i, links[i]) for i in range(6)]
        assert [button_url(app.tg.edits[i][2].reply_markup.rows[0].buttons[0]) for i in range(6)] == links
        posts = await app.db.sent_posts(1)
        assert [p.buttons[0][0]["u"] for p in posts] == links
        assert [p.buttons[0][0]["t"] for p in posts][-1] == "Download Episodes 101 to 114"
        assert 1 not in app.ctx.state  # nothing left to answer
        await app.db.close()

    run(go())


def test_the_short_end_gets_its_own_post_or_joins_the_last(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopost 102 20")
        await app.press(app.out.callback_data("Anime Channel"))
        texts = [e[2].splitlines()[0] for e in app.tg.sent if e[0] == "text" and e[1].channel_id == 1]
        assert texts == ["Episodes 01 to 20", "Episodes 21 to 40", "Episodes 41 to 60", "Episodes 61 to 80", "Episodes 81 to 102"]
        await app.db.close()

    run(go())


def test_admins_may_use_it_and_strangers_may_not(tmp_path):
    async def go():
        app = await make_app(tmp_path, admins=frozenset({7}))
        app.uid = 7
        await app.text("/autopost 40 20")
        assert "Which channel" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Anime Channel"))
        assert len([e for e in app.tg.sent if e[0] == "text" and e[1].channel_id == 1]) == 2
        app.uid = 99
        before = len(app.tg.sent)
        await app.text("/autopost 40 20")
        assert "private" in app.out.last_text and len(app.tg.sent) == before
        await app.db.close()

    run(go())


def test_wrong_numbers_and_no_channels_are_explained(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopost")
        assert "Usage" in norm(app.out.last_text) and "/autopost 114 20" in norm(app.out.last_text)
        for bad in ("114", "x y", "0 5", "114 20 5"):
            await app.text(f"/autopost {bad}")
            assert "Usage: /autopost <total episodes> <interval>" in norm(app.out.last_text), bad
        await app.text("/autopost 5000 1")
        assert "at most 200" in app.out.last_text
        assert not [e for e in app.tg.sent if e[0] == "text"]  # nothing was posted by any of those

        (tmp_path / "empty").mkdir()
        empty = App(tmp_path / "empty", edit_delay=0)
        await empty.start()
        await empty.text("/autopost 114 20")
        assert "No channels yet" in empty.out.last_text
        await empty.db.close()
        await app.db.close()

    run(go())


def test_cancelling_the_channel_choice_posts_nothing(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopost 114 20")
        await app.press(app.out.callback_data("Cancel"))
        assert "Cancelled" in app.out.last_text
        assert not [e for e in app.tg.sent if e[0] == "text"]
        # an old button does nothing any more
        await app.press("apc:" + "0" * 8 + ":1")
        assert "expired" in app.out.last_text
        await app.db.close()

    run(go())


def test_a_double_press_posts_once(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopost 40 20")
        data = app.out.callback_data("Anime Channel")
        await app.press(data)
        await app.press(data)
        assert "expired" in app.out.last_text
        assert len([e for e in app.tg.sent if e[0] == "text" and e[1].channel_id == 1]) == 2
        await app.db.close()

    run(go())


def test_the_bot_needs_the_post_right_and_a_free_hand(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        app.tg.rights = dict(admin=True, post=False, edit=True, delete=True, invite=False, add_admins=False)
        await app.text("/autopost 40 20")
        pick = app.out.callback_data("Anime Channel")
        await app.press(pick)
        assert "Post messages" in app.out.last_text
        assert not [e for e in app.tg.sent if e[0] == "text"]
        # the plan is still there: with the right given, the same button works - but not while a long job runs
        app.tg.rights["post"] = True
        await app.ctx.lock.acquire()
        await app.press(pick)
        assert "still running" in app.out.last_text
        app.ctx.lock.release()
        await app.press(pick)
        assert len([e for e in app.tg.sent if e[0] == "text" and e[1].channel_id == 1]) == 2
        await app.db.close()

    run(go())


def test_when_telegram_stops_midway_the_posts_made_still_get_their_links(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        real = app.tg.send_message
        calls = []

        async def flaky(peer, message="", **kw):
            calls.append(message)
            if len(calls) == 3:
                raise errors.ChatWriteForbiddenError(request=None)
            return await real(peer, message, **kw)

        app.tg.send_message = flaky
        await app.text("/autopost 114 20")
        await app.press(app.out.callback_data("Anime Channel"))
        # the summary (an edit of the picker) tells where it stopped; the next message asks for the first link
        summary = [norm(t) for k, t, _ in app.out.log if k == "edit" and t and "Stopped early" in t]
        assert summary and "Posted 2 of 6" in summary[0] and "Episodes 41 to 60" in summary[0]
        assert "1 of 2" in norm(app.out.last_text)
        assert len(await app.db.sent_posts(1)) == 2
        await app.text("https://t.me/mybot?start=1")
        await app.text("https://t.me/mybot?start=2")
        assert "All done" in app.out.last_text and "2 link(s) replaced" in app.out.last_text
        await app.db.close()

    run(go())


def test_nothing_posted_means_a_plain_error(tmp_path):
    async def go():
        app = await make_app(tmp_path)

        async def refuse(peer, message="", **kw):
            raise errors.ChatWriteForbiddenError(request=None)

        app.tg.send_message = refuse
        await app.text("/autopost 40 20")
        await app.press(app.out.callback_data("Anime Channel"))
        assert "Nothing was posted" in app.out.last_text and "Episodes 01 to 20" in app.out.last_text
        assert await app.db.sent_posts(1) == []
        await app.db.close()

    run(go())


def test_skipping_and_stopping_while_the_links_are_asked(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopost 80 20")
        await app.press(app.out.callback_data("Anime Channel"))
        await app.text("https://t.me/mybot?start=1")
        assert "2 of 4" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Skip"))  # keeps the placeholder
        assert "Skipped" in app.out.last_text and "3 of 4" in norm(app.out.last_text)
        cancel = app.out.callback_data("Cancel")
        # the Skip button of the earlier message is stale now
        await app.press("lqs:1")
        assert "earlier step" in app.out.last_text
        await app.press(cancel)
        shown = norm(app.out.last_text)
        assert "Stopped" in shown and "1 link(s) replaced" in shown and "1 skipped" in shown and "2 not asked" in shown
        assert 1 not in app.ctx.state
        posts = await app.db.sent_posts(1)
        assert [p.buttons[0][0]["u"] for p in posts] == ["https://t.me/mybot?start=1", PLACEHOLDER_LINK, PLACEHOLDER_LINK, PLACEHOLDER_LINK]
        # nothing more is expected from the user
        await app.text("https://t.me/whatever")
        assert "Use /new" in app.out.last_text
        await app.db.close()

    run(go())


def test_something_that_is_not_a_link_is_asked_again_and_a_refusal_keeps_the_step(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        await app.text("/autopost 40 20")
        await app.press(app.out.callback_data("Anime Channel"))
        await app.text("hello there")
        assert "doesn't look like a link" in norm(app.out.last_text)
        # Telegram refuses the keyboard edit: the saved link is unchanged and the same post is asked again
        app.tg.fail["EditMessageRequest"] = errors.ChatAdminRequiredError(request=None)
        await app.text("t.me/mybot?start=1")
        assert "NOT changed" in app.out.last_text
        assert (await app.db.sent_posts(1))[0].buttons[0][0]["u"] == PLACEHOLDER_LINK
        app.tg.fail.clear()
        await app.text("t.me/mybot?start=1")
        assert "now opens" in app.out.last_text and "https://t.me/mybot?start=1" in app.out.last_text
        assert (await app.db.sent_posts(1))[0].buttons[0][0]["u"] == "https://t.me/mybot?start=1"
        await app.db.close()

    run(go())


# ------------------------------------------------------------------- mass replace links
def sent(app, pid_text, message_id, buttons, channel_id=1, text=None):
    return app.db.create_post(
        channel_id=channel_id, message_id=message_id, status="sent", source="bot", text=text or pid_text,
        entities=[], buttons=buttons, link_preview=False,
    )


def one(label, url):
    return [[{"t": label, "u": url}]]


async def channel_with_posts(app):
    """#1 text, #2 button, #3 text, #4 only another bot's button, #5 button, #6 two buttons, #7 button in ANOTHER channel."""
    await app.db.save_channel(2, 6, "Other", "other", 1)
    posts = {}
    posts[1] = await sent(app, "just text", 10, [])
    posts[2] = await sent(app, "Episodes 01 to 20", 11, one("Download Episodes 01 to 20", "https://t.me/old?start=a"))
    posts[3] = await sent(app, "plain again", 12, [])
    posts[4] = await sent(app, "reactions only", 13, [[{"t": "👍 3", "raw": "AAAA"}]])
    posts[5] = await sent(app, "Episodes 21 to 40", 14, one("Download Episodes 21 to 40", "https://t.me/old?start=b"))
    posts[6] = await sent(
        app, "Choose", 15, [[{"t": "480p", "u": "https://t.me/old?start=c"}, {"t": "720p", "u": "https://t.me/old?start=d"}]],
    )
    posts[7] = await sent(app, "elsewhere", 5, one("Other channel", "https://t.me/old?start=z"), channel_id=2)
    return posts


def test_only_published_posts_with_a_link_button_get_the_option(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        posts = await channel_with_posts(app)
        draft = await app.db.create_post(channel_id=1, status="draft", source="bot", text="d", entities=[], buttons=one("Get", "https://t.me/x"))
        for key, expected in ((1, False), (2, True), (4, False), (5, True), (6, True)):
            await app.press(f"po:{posts[key].id}")
            assert ("Mass replace links" in str(app.out.last_buttons())) is expected, key
        await app.press(f"po:{draft.id}")
        assert "Mass replace links" not in str(app.out.last_buttons())
        await app.press(f"mlp:{draft.id}")
        assert "published posts only" in app.out.last_text
        await app.press(f"mlp:{posts[1].id}")
        assert "no button with a link" in app.out.last_text
        await app.db.close()

    run(go())


def test_mass_replace_walks_through_the_button_posts_of_the_channel(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        posts = await channel_with_posts(app)
        await app.press(f"po:{posts[2].id}")
        await app.press(app.out.callback_data("Mass replace links"))
        ask = norm(app.out.last_text)
        assert "Mass replace links" in ask and "1 of 4" in ask and "Download Episodes 01 to 20" in ask
        assert "https://t.me/old?start=a" in ask and f"Post #{posts[2].id}" in ask
        assert "Cancel" in str(app.out.last_buttons())

        await app.text("https://t.me/new?start=1")
        ask = norm(app.out.last_text)  # the text posts (#3) and the reactions-only post (#4) are skipped
        assert "2 of 4" in ask and "Download Episodes 21 to 40" in ask and f"Post #{posts[5].id}" in ask
        assert "now opens https://t.me/new?start=1" in ask
        await app.text("https://t.me/new?start=2")
        ask = norm(app.out.last_text)
        assert "3 of 4" in ask and "480p" in ask and "https://t.me/old?start=c" in ask
        await app.text("https://t.me/new?start=3")
        assert "4 of 4" in norm(app.out.last_text) and "720p" in norm(app.out.last_text)
        await app.text("https://t.me/new?start=4")
        assert "All done" in app.out.last_text and "4 link(s) replaced" in app.out.last_text

        # keyboard-only edits: the text of the posts was never sent again
        edits = [(mid, req) for kind, mid, req in app.tg.edits if kind == "raw"]
        assert [mid for mid, _ in edits] == [11, 14, 15, 15]
        assert all(req.message is None for _, req in edits)
        last = edits[-1][1].reply_markup.rows[0].buttons
        assert [button_url(b) for b in last] == ["https://t.me/new?start=3", "https://t.me/new?start=4"]
        assert [b.text for b in last] == ["480p", "720p"]

        fresh = {k: await app.db.get_post(p.id) for k, p in posts.items()}
        assert fresh[2].buttons == one("Download Episodes 01 to 20", "https://t.me/new?start=1")
        assert fresh[5].buttons[0][0]["u"] == "https://t.me/new?start=2"
        assert [b["u"] for b in fresh[6].buttons[0]] == ["https://t.me/new?start=3", "https://t.me/new?start=4"]
        assert fresh[4].buttons == [[{"t": "👍 3", "raw": "AAAA"}]]  # another bot's button: untouched
        assert fresh[7].buttons[0][0]["u"] == "https://t.me/old?start=z"  # another channel: untouched
        assert fresh[2].text == "Episodes 01 to 20"
        await app.db.close()

    run(go())


def test_it_starts_at_the_chosen_post_and_goes_towards_the_newest(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        posts = await channel_with_posts(app)
        await app.press(f"mlp:{posts[5].id}")  # #2 is older: not asked
        assert "1 of 3" in norm(app.out.last_text) and "Download Episodes 21 to 40" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Skip"))
        assert "2 of 3" in norm(app.out.last_text) and "480p" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Cancel"))
        shown = norm(app.out.last_text)
        assert "Stopped" in shown and "1 skipped" in shown and "2 not asked" in shown
        assert (await app.db.get_post(posts[5].id)).buttons[0][0]["u"] == "https://t.me/old?start=b"
        assert not [1 for kind, mid, req in app.tg.edits if kind == "raw"]
        await app.db.close()

    run(go())


def test_a_post_that_disappears_meanwhile_is_passed_over(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        posts = await channel_with_posts(app)
        await app.press(f"mlp:{posts[2].id}")
        await app.db.delete_post(posts[5].id)  # forgotten in the bot while the list is open
        await app.text("https://t.me/new?start=1")
        assert "3 of 4" in norm(app.out.last_text) and "480p" in norm(app.out.last_text)  # #5 was passed over
        await app.text("https://t.me/new?start=2")
        await app.text("https://t.me/new?start=3")
        assert "All done" in app.out.last_text
        assert "3 link(s) replaced" in app.out.last_text and "1 button(s) no longer there" in app.out.last_text
        await app.db.close()

    run(go())


def test_telegram_refusing_a_link_keeps_the_same_step(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        posts = await channel_with_posts(app)
        await app.press(f"mlp:{posts[2].id}")
        skip = app.out.callback_data("Skip")
        app.tg.fail["EditMessageRequest"] = errors.MessageIdInvalidError(request=None)
        await app.text("https://t.me/new?start=1")
        assert "NOT changed" in app.out.last_text
        assert (await app.db.get_post(posts[2].id)).buttons[0][0]["u"] == "https://t.me/old?start=a"
        app.tg.fail.clear()
        await app.press(skip)
        assert "2 of 4" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_the_last_button_post_ends_the_list(tmp_path):
    async def go():
        app = await make_app(tmp_path)
        p = await sent(app, "only one", 3, one("Get", "https://t.me/old"))
        await app.press(f"mlp:{p.id}")
        assert "1 of 1" in norm(app.out.last_text)
        await app.text("https://t.me/new")
        assert "All done" in app.out.last_text and "1 link(s) replaced" in app.out.last_text
        await app.db.close()

    run(go())
