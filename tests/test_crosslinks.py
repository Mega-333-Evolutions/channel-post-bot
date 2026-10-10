"""Links to old posts in OTHER connected channels follow the copies after a /repost (and go back when it is undone)."""
import asyncio

from telethon import functions, types

from app.crosslinks import CrossResult, progress_text, relink_after_repost, relink_channels, report_lines, restore_after_undo
from app.postlinks import PostLinks
from app.repost_engine import delete_copies, run_repost
from app.sync_engine import live_fields
from app.tgutil import button_url, ser_entities, ser_markup

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, cb_btn, make_msg, markup, rpc, url_btn

CH, SIDE = "testch", "sidech"  # the reposted channel (id 1) and another connected channel (id 2)
N = 9  # posts in the reposted channel: post i gets the copy i + N
NOTICE = "This message couldn't be displayed on your device due to copyright infringement."


def run(coro):
    return asyncio.run(coro)


def plain_a(n=N):
    return {i: make_msg(i, f"Post {i}", out=True) for i in range(1, n + 1)}


T1 = "Read https://t.me/testch/2 now"


def side_posts():
    """What the other channel says about the posts of the reposted one: typed link, hyperlink, button, private form ..."""
    return {
        1: make_msg(1, T1, [types.MessageEntityBold(T1.index("now"), 3)], out=True),
        2: make_msg(
            2, "Part three",
            [types.MessageEntityTextUrl(0, 4, f"https://t.me/{CH}/3?single"), types.MessageEntityItalic(5, 5)], out=True,
        ),
        3: make_msg(
            3, "Pick one",
            markup=markup(
                [url_btn("Part 4", f"https://t.me/{CH}/4"), url_btn("Other", "https://example.com/x")], [cb_btn("👍 3", b"r1")]
            ),
            out=True,
        ),
        4: make_msg(4, f"Ghost https://t.me/{CH}/99", out=True),  # a post that has no copy: stays
        5: make_msg(5, "Elsewhere https://t.me/other_chan/2", out=True),  # another channel: stays
        6: make_msg(6, f"By a friend https://t.me/{CH}/5", out=False),  # somebody else's post
        7: make_msg(7, "nothing here", out=True),
        8: make_msg(8, "Private https://t.me/c/1/6 form", out=True),
    }


async def save_rows(db, cid, msgs):
    for m in msgs.values():
        if m.out:
            await db.create_post(
                channel_id=cid, message_id=m.id, status="sent", source="bot", created_by=99, link_preview=False,
                **live_fields(m),
            )


async def world(tmp_path, a_msgs=None, b_msgs=None, *, first=1, last=None, save=True):
    """The reposted channel A (id 1) with a repost registered, and the other channel B (id 2)."""
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Test", CH, 99)
    await db.save_channel(2, 6, "Side", SIDE, 99)
    a = FakeChannelClient(plain_a() if a_msgs is None else a_msgs, username=CH, cid=1, title="Test")
    b = a.add_channel(FakeChannelClient(side_posts() if b_msgs is None else b_msgs, username=SIDE, cid=2, title="Side"))
    if save:
        await save_rows(db, 2, b.msgs)
    await db.create_migration(
        "m1", 1, old=None, new=None, include_typed=True, include_posts=False, first_id=first, last_id=last or max(a.msgs),
        partial=False, user_id=99,
    )
    return db, await db.get_channel(1), a, b, await db.get_migration("m1")


def edits_of(chan):
    return [r for r in chan.requests if isinstance(r, functions.messages.EditMessageRequest)]


async def repost(db, ch, a, mig, **kw):
    return await run_repost(a, db, ch, mig, 99, delay=0, settle_pause=0, **kw)


# ============================================================================================ the main promise
def test_links_in_another_channel_point_at_the_copies(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        res = await repost(db, ch, a, mig)
        assert (res.copied_units, res.failed, res.aborted, res.cross_error) == (N, [], None, None)
        assert res.phase == "links" and res.cross is not None
        cross = res.cross

        # a typed link: the digits change, the bold text after it moves along
        assert b.msgs[1].message == "Read https://t.me/testch/11 now"
        bold = b.msgs[1].entities[0]
        assert isinstance(bold, types.MessageEntityBold) and bold.offset == T1.index("now") + 1 and bold.length == 3
        assert b.msgs[1].message[bold.offset : bold.offset + bold.length] == "now"
        # a hyperlink behind text: the url changes, the text and the other formatting stay
        assert b.msgs[2].message == "Part three"
        link, italic = b.msgs[2].entities
        assert link.url == f"https://t.me/{CH}/12?single" and (link.offset, link.length) == (0, 4)
        assert (italic.offset, italic.length) == (5, 5)
        # a button: only that url changes; the other url button and the reaction button stay
        rows = b.msgs[3].reply_markup.rows
        assert button_url(rows[0].buttons[0]) == f"https://t.me/{CH}/13" and rows[0].buttons[0].text == "Part 4"
        assert button_url(rows[0].buttons[1]) == "https://example.com/x" and rows[1].buttons[0].text == "👍 3"
        # a post of somebody else (the bot may edit those here) and the private form of the link
        assert b.msgs[6].message == f"By a friend https://t.me/{CH}/14"
        assert b.msgs[8].message == "Private https://t.me/c/1/15 form"
        # no copy, other channel, no link: untouched
        assert b.msgs[4].message == f"Ghost https://t.me/{CH}/99"
        assert b.msgs[5].message == "Elsewhere https://t.me/other_chan/2" and b.msgs[7].message == "nothing here"

        edits = edits_of(b)
        assert [r.id for r in edits] == [1, 2, 3, 6, 8]  # nothing else was edited
        assert edits[2].message is None  # the button post: only its keyboard was sent, the text stays as it is
        assert cross.relinked == 5 and cross.links == 5 and cross.failed == [] and cross.not_allowed == 0
        assert [c.channel.id for c in cross.changed_channels] == [2] and cross.clean and not cross.stopped
        # the originals in the reposted channel were not touched
        assert all(a.msgs[i].message == f"Post {i}" for i in range(1, N + 1))
        assert not edits_of(a)

        # My posts shows what the channel shows now
        for i in (1, 2, 3, 8):
            row = await db.find_by_message(2, i)
            live = b.msgs[i]
            assert row.text == live.message and row.entities == ser_entities(live.entities)
            assert row.buttons == ser_markup(live.reply_markup)
        assert (await db.find_by_message(2, 3)).text == "Pick one"
        assert await db.find_by_message(2, 6) is None  # somebody else's post was never saved, and still isn't
        assert await db.has_mark("m1", "links")
        await db.close()

    run(go())


def test_the_report_says_what_was_done(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        res = await repost(db, ch, a, mig)
        text = "\n".join(report_lines(res.cross))
        assert "5 link(s) in 5 post(s) of 1 channel(s) now point at the new copies" in text
        assert "Side: 5 link(s) in 5 post(s)" in text
        assert "Stopped" not in text and "could not" not in text
        await db.close()

    run(go())


def test_running_it_again_changes_nothing(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        await repost(db, ch, a, mig)
        before = len(edits_of(b))
        again = await relink_after_repost(a, db, ch, mig, delay=0, settle_pause=0, pause=0)
        assert again.relinked == 0 and again.links == 0 and again.clean
        assert len(edits_of(b)) == before  # not one more edit
        assert "no link there needed changing" in "\n".join(report_lines(again))
        await db.close()

    run(go())


def test_a_link_that_appears_later_is_fixed_by_the_retry(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        await repost(db, ch, a, mig)
        # an admin posts a new message that still links to an old post (the old post is deleted by now)
        b._new(f"New: https://t.me/{CH}/7", None, None).out = False
        newest = max(b.msgs)
        again = await relink_after_repost(a, db, ch, mig, delay=0, settle_pause=0, pause=0)
        assert again.relinked == 1 and b.msgs[newest].message == f"New: https://t.me/{CH}/16"
        await db.close()

    run(go())


# ================================================================================ the reposted channel itself
def test_older_posts_of_the_channel_itself_are_fixed_but_the_originals_are_not(tmp_path):
    async def go():
        msgs = plain_a()
        msgs[2] = make_msg(2, f"Old post: https://t.me/{CH}/7", out=True)  # outside the copied range
        msgs[7] = make_msg(7, f"Seven: https://t.me/{CH}/8", out=True)  # an original that is copied (and later deleted)
        db, ch, a, b, mig = await world(tmp_path, msgs, first=5, last=9)
        res = await repost(db, ch, a, mig)
        assert res.copied_units == 5 and res.failed == []
        # copies of 5..9 are 10..14 (7 -> 12, 8 -> 13)
        assert a.msgs[2].message == f"Old post: https://t.me/{CH}/12"  # the post that stays points at the copy
        assert a.msgs[7].message == f"Seven: https://t.me/{CH}/8"  # the original stays exactly as it was
        assert a.msgs[12].message == f"Seven: https://t.me/{CH}/13"  # its copy points at the copy of 8
        assert {r.id for r in edits_of(a)} == {2, 12}
        # in the other channel only the links to the copied posts (5..9) change
        assert b.msgs[1].message == "Read https://t.me/testch/2 now"  # 2 was not copied
        assert button_url(b.msgs[3].reply_markup.rows[0].buttons[0]) == f"https://t.me/{CH}/4"  # neither was 4
        assert b.msgs[6].message == f"By a friend https://t.me/{CH}/10"  # 5 -> 10
        assert b.msgs[8].message == "Private https://t.me/c/1/11 form"  # 6 -> 11
        await db.close()

    run(go())


# ==================================================================================== changes made meanwhile
def test_a_post_changed_meanwhile_is_read_again_before_it_is_edited(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path, b_msgs={
            1: make_msg(1, T1, [types.MessageEntityBold(T1.index("now"), 3)], out=True),
            2: make_msg(2, "Part three", [types.MessageEntityTextUrl(0, 4, f"https://t.me/{CH}/3")], out=True),
        })
        for i in range(1, N + 1):
            a._new(f"Copy {i}", None, None)  # the copies exist: ids 10..18
        links = PostLinks.for_channel(ch, {i: i + N for i in range(1, N + 1)})
        fired = []

        async def watch(c):
            if c.phase == "edit" and not fired and c.current.channel.id == 2:
                fired.append(1)
                # while the bot works an admin rewrites post 1 and fixes the link of post 2 by hand
                b.msgs[1].message = "Read https://t.me/testch/2 now - updated"
                b.msgs[2].entities = [types.MessageEntityTextUrl(0, 4, f"https://t.me/{CH}/12")]

        res = await relink_channels(a, db, links, [await db.get_channel(2)], delay=0, settle_pause=0, pause=0, progress=watch)
        assert fired
        assert b.msgs[1].message == "Read https://t.me/testch/11 now - updated"  # the new wording is kept, link fixed
        assert [r.id for r in edits_of(b)] == [1]  # post 2 was already right: not edited
        assert res.relinked == 1 and res.links == 1
        await db.close()

    run(go())


def test_a_post_deleted_meanwhile_is_not_a_problem(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        for i in range(1, N + 1):
            a._new(f"Copy {i}", None, None)
        links = PostLinks.for_channel(ch, {i: i + N for i in range(1, N + 1)})
        fired = []

        async def watch(c):
            if c.phase == "edit" and not fired:
                fired.append(1)
                del b.msgs[1]

        res = await relink_channels(a, db, links, [await db.get_channel(2)], delay=0, settle_pause=0, pause=0, progress=watch)
        assert res.channels[0].gone == 1 and res.relinked == 4 and res.failed == []
        await db.close()

    run(go())


# ======================================================================================== rights and refusals
def test_somebody_elses_posts_need_the_edit_right(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        b.rights = dict(admin=True, post=True, edit=False, delete=True, invite=False, add_admins=False)
        res = await repost(db, ch, a, mig)
        cross = res.cross
        assert b.msgs[6].message == f"By a friend https://t.me/{CH}/5"  # not touched
        assert b.msgs[1].message == "Read https://t.me/testch/11 now"  # the bot's own posts are
        assert cross.relinked == 4 and cross.not_allowed == 1 and not cross.clean
        text = "\n".join(report_lines(cross))
        assert "1 post(s) with such a link were made by somebody else" in text and "Side" in text
        assert "Edit messages of others" in text and "Update links again" in text
        await db.close()

    run(go())


def test_what_telegram_refuses_is_reported_and_the_rest_goes_on(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        b.msgs[2].via_other_bot = True  # its buttons / text belong to another bot: only that bot may edit it
        res = await repost(db, ch, a, mig)
        cross = res.cross
        assert [(c.id, mid, name) for c, mid, name in cross.failed] == [(2, 2, "MessageIdInvalidError")]
        assert b.msgs[2].entities[0].url == f"https://t.me/{CH}/3?single"
        assert button_url(b.msgs[3].reply_markup.rows[0].buttons[0]) == f"https://t.me/{CH}/13"
        assert cross.relinked == 4 and not cross.clean
        text = "\n".join(report_lines(cross))
        assert "1 post(s) could not get their links updated (Side #2 MessageIdInvalidError)" in text
        assert "other bots" in text
        await db.close()

    run(go())


def test_the_same_refusal_over_and_over_gives_up_on_that_channel_only(tmp_path):
    async def go():
        many = {i: make_msg(i, f"Link https://t.me/{CH}/{(i % N) + 1}", out=True) for i in range(1, 13)}
        db, ch, a, b, mig = await world(tmp_path, b_msgs=many)
        for m in b.msgs.values():
            m.via_other_bot = True
        res = await repost(db, ch, a, mig)
        side = [c for c in res.cross.channels if c.channel.id == 2][0]
        assert side.aborted == "MessageIdInvalidError" and len(side.failed) == 5  # not twelve useless tries
        assert "Gave up on Side" in "\n".join(report_lines(res.cross))
        await db.close()

    run(go())


def test_channels_that_cannot_be_used_are_skipped_and_named(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        specs = {
            3: ("Not admin", dict(rights=dict(admin=False))),
            4: ("Struck", dict(restriction=NOTICE)),
            5: ("Private now", dict(fail={"GetFullChannelRequest": rpc("ChannelPrivateError")})),
            6: ("No rights", dict(rights=dict(admin=True, post=False, edit=False))),
        }
        extra = {}
        for cid, (title, sw) in specs.items():
            await db.save_channel(cid, 7, title, None, 99)
            c = a.add_channel(FakeChannelClient({1: make_msg(1, f"x https://t.me/{CH}/2", out=True)}, cid=cid, title=title))
            for k, v in sw.items():
                setattr(c, k, v)
            extra[cid] = c
        res = await repost(db, ch, a, mig)
        skipped = {c.channel.id: c.skipped for c in res.cross.skipped}
        assert set(skipped) == {3, 4, 5, 6}
        assert "not an admin" in skipped[3] and "restricts" in skipped[4] and "copyright" in skipped[4]
        assert skipped[5] and "no right" in skipped[6]
        assert all(not edits_of(c) for c in extra.values())  # nothing was edited there
        assert b.msgs[1].message == "Read https://t.me/testch/11 now"  # the usable channels were done
        text = "\n".join(report_lines(res.cross))
        assert "Not looked at:" in text and "Struck" in text and "Not admin" in text
        await db.close()

    run(go())


def test_posts_telegram_holds_back_are_left_alone(tmp_path):
    async def go():
        held = make_msg(
            9, NOTICE, markup=markup([url_btn("Part 2", f"https://t.me/{CH}/2")]), out=True
        )
        held.restriction_reason = [types.RestrictionReason(platform="all", reason="copyright", text=NOTICE)]
        notice_only = make_msg(10, NOTICE, out=True)  # the notice as the text, no mark
        msgs = side_posts()
        msgs[9], msgs[10] = held, notice_only
        db, ch, a, b, mig = await world(tmp_path, b_msgs=msgs)
        res = await repost(db, ch, a, mig)
        side = [c for c in res.cross.channels if c.channel.id == 2][0]
        assert side.held_back == 2 and side.relinked == 5
        assert button_url(b.msgs[9].reply_markup.rows[0].buttons[0]) == f"https://t.me/{CH}/2"
        assert b.msgs[9].message == NOTICE and b.msgs[10].message == NOTICE
        assert "2 post(s) are held back by Telegram" in "\n".join(report_lines(res.cross))
        await db.close()

    run(go())


# ============================================================================================ stop and go on
def test_stopping_leaves_the_rest_for_the_retry(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        await db.save_channel(3, 7, "Third", None, 99)
        c3 = a.add_channel(FakeChannelClient({1: make_msg(1, f"see https://t.me/{CH}/2", out=True)}, cid=3, title="Third"))
        for i in range(1, N + 1):
            a._new(f"Copy {i}", None, None)
        links = PostLinks.for_channel(ch, {i: i + N for i in range(1, N + 1)})
        chans = [await db.get_channel(2), await db.get_channel(3)]
        stop = []

        async def watch(c):
            if c.index == 2:
                stop.append(1)

        res = await relink_channels(
            a, db, links, chans, delay=0, settle_pause=0, pause=0, progress=watch, should_stop=lambda: bool(stop)
        )
        assert res.stopped and not res.clean
        assert b.msgs[1].message == "Read https://t.me/testch/11 now"  # the first channel was done
        assert c3.msgs[1].message == f"see https://t.me/{CH}/2"  # the second was not
        assert "Stopped before every channel was done" in "\n".join(report_lines(res))
        again = await relink_channels(a, db, links, chans, delay=0, settle_pause=0, pause=0)
        assert again.relinked == 1 and c3.msgs[1].message == f"see https://t.me/{CH}/11" and again.clean
        await db.close()

    run(go())


def test_the_progress_text_names_the_channel_and_the_count(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        seen = []

        async def watch(c):
            seen.append(progress_text(c))

        links = PostLinks.for_channel(ch, {2: 11})
        await relink_channels(
            a, db, links, await db.list_channels(), delay=0, settle_pause=0, pause=0, progress=watch, result=CrossResult()
        )
        joined = "\n".join(seen)
        assert "Side" in joined and "Test" in joined and "(1 of 2)" in joined and "(2 of 2)" in joined
        assert "Reading posts" in joined
        await db.close()

    run(go())


def test_nothing_is_read_when_there_is_nothing_to_point_at(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        res = await relink_channels(
            a, db, PostLinks.for_channel(ch, {}), await db.list_channels(), delay=0, settle_pause=0, pause=0
        )
        assert res.channels == [] and not a.requests and not b.requests
        await db.close()

    run(go())


# ============================================================================================ undo goes back
def test_undo_points_the_links_back_at_the_original_posts(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        original = {i: (m.message, ser_entities(m.entities), ser_markup(m.reply_markup)) for i, m in b.msgs.items()}
        await repost(db, ch, a, mig)
        assert b.msgs[1].message != original[1][0]
        back = await restore_after_undo(a, db, ch, mig, delay=0, settle_pause=0, pause=0)
        assert back.relinked == 5 and back.clean
        for i, (text, ents, buttons) in original.items():
            assert b.msgs[i].message == text, i
            assert ser_entities(b.msgs[i].entities) == ents, i
            assert ser_markup(b.msgs[i].reply_markup) == buttons, i
        # and then the copies can go
        res = await delete_copies(a, db, ch, mig, "new", delay=0)
        assert res.remaining == 0 and res.deleted == N
        await db.close()

    run(go())


def test_an_undo_without_changed_links_reads_nothing(tmp_path):
    async def go():
        db, ch, a, b, mig = await world(tmp_path)
        back = await restore_after_undo(a, db, ch, mig, delay=0, settle_pause=0, pause=0)  # no mark: nothing was changed
        assert back.channels == [] and not a.requests and not b.requests
        await db.close()

    run(go())


# ======================================================================================== as the owner sees it
async def make_app(tmp_path, a_msgs=None, b_msgs=None):
    from .harness import App
    from .test_repost_flow import fill

    app = App(tmp_path, edit_delay=0.0)
    await app.start()
    await app.db.save_channel(1, 5, "Test", CH, 1)
    await app.db.save_channel(2, 6, "Side", SIDE, 1)
    fill(app.tg, plain_a() if a_msgs is None else a_msgs)
    app.tg.username = CH
    side = app.tg.add_channel(FakeChannelClient(side_posts() if b_msgs is None else b_msgs, cid=2, username=SIDE, title="Side"))
    await save_rows(app.db, 2, side.msgs)
    return app, side


async def copy_everything(app):
    await app.text("/repost --no-typed")  # no usernames to swap: only the posts are copied
    await app.press(app.out.callback_data("Test"))
    await app.press(app.out.callback_data("Start copying"))


def test_the_plan_promises_the_links_of_all_channels(tmp_path):
    from .harness import norm

    async def go():
        app, side = await make_app(tmp_path)
        await app.text("/repost --no-typed")
        assert "Which channel" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Test"))
        text = norm(app.out.last_text)
        assert "Repost plan" in text and "link to an old post" in text and "ANY of your connected channels" in text
        await app.db.close()

    run(go())


def test_the_finished_repost_tells_what_happened_in_the_other_channels(tmp_path):
    from .harness import norm

    async def go():
        app, side = await make_app(tmp_path)
        await copy_everything(app)
        text = norm(app.out.last_text)
        assert "all posts copied in order" in text
        assert "5 link(s) in 5 post(s) of 1 channel(s) now point at the new copies" in text and "Side: 5 link(s)" in text
        labels = [t for row in app.out.last_buttons() for t in row]
        assert any("Update links again" in t for t in labels) and any("Delete the old posts" in t for t in labels)
        assert side.msgs[1].message == "Read https://t.me/testch/11 now"
        await app.db.close()

    run(go())


def test_update_links_again_finds_what_came_later(tmp_path):
    from .harness import norm

    async def go():
        app, side = await make_app(tmp_path)
        await copy_everything(app)
        again = app.out.callback_data("Update links again")
        assert again.startswith("rpl:")
        await app.press(again)  # nothing new: nothing to change
        assert "no link there needed changing" in norm(app.out.last_text)
        m = side._new(f"Late: https://t.me/{CH}/8", None, None)
        m.out = False
        await app.press(app.out.callback_data("Update links again"))
        assert side.msgs[m.id].message == f"Late: https://t.me/{CH}/17"
        assert "1 link(s) in 1 post(s) of 1 channel(s)" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_undo_points_the_links_back_and_says_so(tmp_path):
    from .harness import norm

    async def go():
        app, side = await make_app(tmp_path)
        await copy_everything(app)
        assert side.msgs[1].message == "Read https://t.me/testch/11 now"
        await app.press(app.out.callback_data("Undo"))
        await app.press(app.out.callback_data("Yes, remove the copies"))
        text = norm(app.out.last_text)
        assert "Done" in text and "5 link(s) in 5 post(s) of 1 channel(s) point back at the original posts" in text
        assert side.msgs[1].message == T1 and side.msgs[8].message == "Private https://t.me/c/1/6 form"
        assert button_url(side.msgs[3].reply_markup.rows[0].buttons[0]) == f"https://t.me/{CH}/4"
        assert sorted(app.tg.msgs) == list(range(1, N + 1))  # only the originals are left
        await app.db.close()

    run(go())


def test_after_the_old_posts_are_deleted_the_links_can_still_be_updated(tmp_path):
    from .harness import norm

    async def go():
        app, side = await make_app(tmp_path)
        await copy_everything(app)
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        assert "Done" in norm(app.out.last_text)
        labels = [t for row in app.out.last_buttons() for t in row]
        assert any("Update links again" in t for t in labels)
        await app.press(app.out.callback_data("Update links again"))
        text = norm(app.out.last_text)
        assert "no link there needed changing" in text and "Test" in text
        await app.db.close()

    run(go())
