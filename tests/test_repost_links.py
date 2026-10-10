"""/repost keeps what a plain copy would lose: answers (replies), pins, and links to other posts of the channel."""
import asyncio

from telethon import functions, types

from app.repost_engine import RepostOptions, plan_repost, relink_copies, run_repost
from app.tgutil import button_url

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, make_msg, markup, photo_media, service_msg, url_btn
from .test_repost import edit_requests, forwarded_ids, setup

CH = "testch"  # the channel's username in the database (see setup())


def build_linked():
    """Posts that point at each other, answer each other and are pinned."""
    return {
        1: make_msg(1, "Intro"),
        2: make_msg(2, "Next: part five", [types.MessageEntityTextUrl(6, 9, f"https://t.me/{CH}/5")]),  # forward reference
        3: make_msg(
            3,
            f"Index: https://t.me/{CH}/1 and https://t.me/c/1/2 and https://t.me/other_chan/2 end",
            [types.MessageEntityBold(len("Index: https://t.me/testch/1 and https://t.me/c/1/2 and https://t.me/other_chan/2 "), 3)],
            markup([url_btn("Go to 4", f"https://t.me/{CH}/4?single")]),  # forward reference in a button
        ),
        4: make_msg(4, "Fourth", reply_to=3),
        5: make_msg(5, "Fifth", [types.MessageEntityTextUrl(0, 5, f"https://t.me/{CH}/1?single")], pinned=True, reply_to=4),
        6: service_msg(6),
        7: make_msg(7, "", media=photo_media(71), grouped_id=70, reply_to=5),
        8: make_msg(8, "album caption", media=photo_media(72), grouped_id=70),
        9: make_msg(9, f"Ghost https://t.me/{CH}/999", []),  # a post that never existed: left as it is
        10: make_msg(10, "Last", pinned=True),
    }


def run(coro):
    return asyncio.run(coro)


async def linked_setup(tmp_path, chan=None, **kw):
    chan = chan or FakeChannelClient(build_linked())
    kw.setdefault("old", None)
    kw.setdefault("new", None)
    return await setup(tmp_path, chan, **kw)


def new_posts(chan, above=10):
    return {i: m for i, m in chan.msgs.items() if i > above and not isinstance(m, types.MessageService)}


def reply_of(m):
    return m.reply_to.reply_to_msg_id if m.reply_to else None


def test_the_plan_tells_what_will_be_kept(tmp_path):
    async def go():
        db, ch, chan, _ = await linked_setup(tmp_path)
        plan = await plan_repost(chan, ch, RepostOptions())
        assert plan.error is None and plan.units == 8
        assert plan.pinned == 2 and plan.replies == 3
        assert (plan.post_link_posts, plan.post_links) == (4, 6)  # posts 2, 3 (three links), 5 and 9
        await db.close()

    run(go())


def test_replies_pins_and_links_follow_the_copies(tmp_path):
    async def go():
        db, ch, chan, mig = await linked_setup(tmp_path)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.copied_messages, res.failed, res.aborted) == (8, 9, [], None)
        new = new_posts(chan)
        assert sorted(new) == list(range(11, 11 + 9 + 0)) or sorted(new) == [11, 12, 13, 14, 15, 16, 17, 18, 19]

        # answers: the copy of 4 answers the copy of 3, 5 answers 4, the album answers 5
        assert reply_of(new[11]) is None and reply_of(new[12]) is None and reply_of(new[13]) is None
        assert (reply_of(new[14]), reply_of(new[15]), reply_of(new[16]), reply_of(new[17])) == (13, 14, 15, 15)
        assert (res.replies, res.reply_lost, res.reply_dropped) == (3, 0, 0)

        # links typed in the text and hyperlinks: backward references were right from the start ...
        assert new[13].message == f"Index: https://t.me/{CH}/11 and https://t.me/c/1/12 and https://t.me/other_chan/2 end"
        bold = new[13].entities[0]
        assert new[13].message.encode("utf-16-le")[bold.offset * 2 : (bold.offset + bold.length) * 2].decode("utf-16-le") == "end"
        link5 = [e for e in new[15].entities if isinstance(e, types.MessageEntityTextUrl)][0]
        assert link5.url == f"https://t.me/{CH}/11?single" and new[15].message == "Fifth"
        # ... forward references were fixed afterwards (post 2 -> 5 is now 15, the button 4 is now 14)
        link2 = [e for e in new[12].entities if isinstance(e, types.MessageEntityTextUrl)][0]
        assert link2.url == f"https://t.me/{CH}/15" and (link2.offset, link2.length) == (6, 9)
        assert button_url(new[13].reply_markup.rows[0].buttons[0]) == f"https://t.me/{CH}/14?single"
        assert new[13].reply_markup.rows[0].buttons[0].text == "Go to 4"
        assert new[18].message == f"Ghost https://t.me/{CH}/999"  # no copy of 999: untouched
        fin = res.final
        assert (fin.checked, fin.relinked, fin.links, fin.failed) == (9, 2, 2, [])
        assert len(edit_requests(chan)) == 2  # only the two posts with a forward reference are edited

        # pins: the copies of the pinned posts, oldest first; the originals stay pinned; no notice is left behind
        assert (fin.pin_wanted, fin.pinned, fin.pin_error) == (2, 2, None)
        assert chan.pinned_order == [15, 19]
        assert new[15].pinned and new[19].pinned and not new[11].pinned
        assert chan.msgs[5].pinned and chan.msgs[10].pinned
        assert [m.id for m in chan.msgs.values() if isinstance(m, types.MessageService)] == [6]

        # My posts shows exactly what the channel shows
        p12, p13 = await db.find_by_message(1, 12), await db.find_by_message(1, 13)
        assert p12.entities[0]["u"] == f"https://t.me/{CH}/15"
        assert p13.buttons == [[{"t": "Go to 4", "u": f"https://t.me/{CH}/14?single"}]]
        assert p13.text == new[13].message
        await db.close()

    run(go())


def test_what_needs_no_change_is_still_copied_by_telegram_itself(tmp_path):
    async def go():
        db, ch, chan, mig = await linked_setup(tmp_path)
        await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        # intro, the forward reference (fixed afterwards), the ghost link and the last post: no reply, no buttons
        assert forwarded_ids(chan) == [[1], [2], [9], [10]]
        reqs = [r for r in chan.requests if isinstance(r, functions.messages.SendMessageRequest)]
        assert [r.reply_to.reply_to_msg_id for r in reqs if r.reply_to] == [13, 14]
        await db.close()

    run(go())


def test_running_it_again_changes_nothing(tmp_path):
    async def go():
        db, ch, chan, mig = await linked_setup(tmp_path)
        await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        edits, requests = len(edit_requests(chan)), len(chan.requests)
        again = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (again.copied_units, again.skipped_done) == (0, 8)
        assert (again.final.relinked, again.final.pin_wanted, again.final.pinned) == (0, 0, 0)
        assert len(edit_requests(chan)) == edits
        assert not any(isinstance(r, functions.messages.UpdatePinnedMessageRequest) for r in chan.requests[requests:])
        await db.close()

    run(go())


def test_a_channel_that_forbids_forwarding_gets_the_same_result(tmp_path):
    async def go():
        chan = FakeChannelClient(build_linked())
        chan.restrict_forwards = True
        db, ch, chan, mig = await linked_setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.failed, res.aborted, res.forwarded, res.rebuilt) == ([], None, 0, 8)
        new = new_posts(chan)
        assert (reply_of(new[14]), reply_of(new[15]), reply_of(new[16])) == (13, 14, 15)
        assert chan.pinned_order == [15, 19]
        assert res.final.relinked == 2
        await db.close()

    run(go())


def test_a_post_waits_for_the_post_it_answers(tmp_path):
    """The parent's copy fails (Telegram never shows its buttons): the child is not copied as a loose post."""

    async def go():
        chan = FakeChannelClient(
            {
                1: make_msg(1, "Parent", markup=markup([url_btn("Get", "https://t.me/goku?start=P")])),
                2: make_msg(2, "Child", reply_to=1),
                3: make_msg(3, "Other"),
            }
        )
        chan.drop_all_markup = True
        db, ch, chan, mig = await linked_setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert [n for _, n in res.failed] == ["ButtonsNotShown", "ReplyParentMissing"]
        assert res.copied_units == 1 and res.final.relinked == 0  # only post 3 was copied
        assert [m.message for m in new_posts(chan, 3).values()] == ["Other"]
        chan.drop_all_markup = False  # Telegram behaves again: "Try the rest again"
        res2 = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res2.copied_units, res2.failed) == (2, [])
        new = new_posts(chan, 3)
        parent, child = [m for m in new.values() if m.message == "Parent"][0], [m for m in new.values() if m.message == "Child"][0]
        assert reply_of(child) == parent.id
        await db.close()

    run(go())


def test_a_reply_to_a_post_outside_a_trial_run_is_copied_without_the_reply(tmp_path):
    async def go():
        chan = FakeChannelClient({1: make_msg(1, "Parent"), 2: make_msg(2, "Child", reply_to=1), 3: make_msg(3, "Gone parent", reply_to=99)})
        db, ch, chan, mig = await linked_setup(tmp_path, chan, first_id=2, partial=True)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.replies, res.reply_dropped) == (2, [], 0, 2)
        assert all(reply_of(m) is None for m in new_posts(chan, 3).values())
        await db.close()

    run(go())


def test_media_that_cannot_be_rebuilt_is_copied_without_its_reply(tmp_path):
    async def go():
        chan = FakeChannelClient({1: make_msg(1, "Parent"), 2: make_msg(2, "Odd", media=types.MessageMediaUnsupported(), reply_to=1)})
        db, ch, chan, mig = await linked_setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.reply_lost) == (2, [], 1)
        assert forwarded_ids(chan) == [[1], [2]]
        await db.close()

    run(go())


def test_pinning_needs_a_right_the_bot_may_lack(tmp_path):
    async def go():
        chan = FakeChannelClient(build_linked())
        chan.rights = dict(admin=True, post=True, edit=False, delete=True, invite=False, add_admins=False)
        db, ch, chan, mig = await linked_setup(tmp_path, chan)
        res = await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.failed, res.aborted, res.copied_units) == ([], None, 8)  # the copies are fine
        assert (res.final.pin_wanted, res.final.pinned, res.final.pin_error) == (2, 0, "ChatAdminRequiredError")
        assert chan.pinned_order == []
        await db.close()

    run(go())


def test_links_in_copies_made_earlier_can_be_fixed_afterwards(tmp_path):
    """A finished repost whose originals are gone: the copies still point at the old ids (the links in the copies are pointed at the new ids afterwards)."""

    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Test", CH, 99)
        ch = await db.get_channel(1)
        text = f"Part 1 https://t.me/{CH}/3 and https://t.me/{CH}/4 and https://t.me/{CH}/50"
        chan = FakeChannelClient(
            {
                11: make_msg(11, "one", out=True),
                12: make_msg(12, text, out=True, markup=markup([url_btn("Two", f"https://t.me/{CH}/1")])),
                13: make_msg(13, "three", out=True),
                14: make_msg(14, "four", out=True),
            }
        )
        ids = {1: 11, 2: 12, 3: 13, 4: 14}
        await db.create_post(channel_id=1, message_id=12, status="sent", source="bot", text=text, entities=[], buttons=[[{"t": "Two", "u": f"https://t.me/{CH}/1"}]])
        dry = await relink_copies(chan, db, ch, ids, [11, 12, 13, 14], dry_run=True)
        assert (dry.checked, dry.relinked, dry.links) == (4, 1, 3) and chan.edits == []
        res = await relink_copies(chan, db, ch, ids, [11, 12, 13, 14], delay=0, settle_pause=0)
        assert (res.relinked, res.links, res.failed) == (1, 3, [])
        assert chan.msgs[12].message == f"Part 1 https://t.me/{CH}/13 and https://t.me/{CH}/14 and https://t.me/{CH}/50"
        assert button_url(chan.msgs[12].reply_markup.rows[0].buttons[0]) == f"https://t.me/{CH}/11"
        saved = await db.find_by_message(1, 12)
        assert saved.text == chan.msgs[12].message and saved.buttons[0][0]["u"].endswith("/11")
        # nothing left to do the second time
        again = await relink_copies(chan, db, ch, ids, [11, 12, 13, 14], delay=0, settle_pause=0)
        assert (again.relinked, again.links) == (0, 0)
        # a copy that was deleted in the meantime is reported, not an error
        del chan.msgs[14]
        gone = await relink_copies(chan, db, ch, ids, [11, 12, 13, 14], delay=0, settle_pause=0)
        assert gone.missing == 1
        await db.close()

    run(go())


def test_the_copies_of_every_repost_are_listed_oldest_first_and_undone_ones_left_out(tmp_path):
    async def go():
        db, ch, chan, mig = await linked_setup(tmp_path)
        await run_repost(chan, db, ch, mig, 99, delay=0, settle_pause=0)
        pairs = await db.migration_pairs(channel_id=1)
        assert pairs == [(1, 11), (2, 12), (3, 13), (4, 14), (5, 15), (7, 16), (8, 17), (9, 18), (10, 19)]
        assert await db.migration_pairs(mid="m1") == pairs and await db.migration_pairs(mid="nope") == []
        await db.mark_migration_deleted("m1", 1, "new", [18, 19])
        assert await db.migration_pairs(channel_id=1) == pairs[:-2]
        assert len(await db.migration_pairs(channel_id=1, alive_only=False)) == 9
        await db.close()

    run(go())


# ------------------------------------------------------------------------------ as the owner sees it
def test_the_plan_and_the_result_message_mention_replies_links_and_pins(tmp_path):
    from .harness import norm
    from .test_repost_flow import make_app, pick

    async def go():
        app = await make_app(tmp_path, build_linked())
        await pick(app)
        plan = norm(app.out.last_text)
        assert "6 link(s) in 4 post(s) point at other posts of this channel" in plan
        assert "3 post(s) answer another post" in plan and "2 pinned post(s)" in plan
        assert "pinned state" not in plan  # it is kept now
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "all posts copied in order" in done
        assert "3 post(s) answer the copy of the post they answered" in done
        assert "2 link(s) in 2 post(s) now point at the new copies" in done
        assert "2 pinned post(s) are pinned again" in done
        assert app.tg.pinned_order == [15, 19]
        await app.db.close()

    asyncio.run(go())


def test_a_missing_pin_right_is_explained_in_the_result(tmp_path):
    from .harness import norm
    from .test_repost_flow import make_app, pick

    async def go():
        app = await make_app(tmp_path, build_linked())
        app.tg.rights = dict(admin=True, post=True, edit=False, delete=True, invite=False, add_admins=False)
        await pick(app)
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "2 copy/copies could not be pinned (ChatAdminRequiredError)" in done and "Edit messages of others" in done
        assert "all posts copied in order" in done  # the copy itself is complete
        await app.db.close()

    asyncio.run(go())


async def finished_repost(app, deleted_originals=True):
    """A repost that is over: the copies (ids 11-13) still point at the old ids 1-3, the originals are gone."""
    text = f"Part one: https://t.me/{CH}/3 and https://t.me/{CH}/2"
    app.tg.msgs = {
        11: make_msg(11, "one", out=True),
        12: make_msg(12, text, out=True, markup=markup([url_btn("Back", f"https://t.me/{CH}/1")])),
        13: make_msg(13, "three", out=True),
    }
    app.tg._top = 13
    await app.db.create_migration("m9", 1, old=None, new=None, include_typed=True, include_posts=False, first_id=1, last_id=3, partial=False, user_id=1)
    rows = [{"old_id": o, "new_id": n, "post": dict(text="", entities=[], buttons=[], media_kind=None, media_file_id=None, link_preview=False)} for o, n in ((1, 11), (2, 12), (3, 13))]
    await app.db.record_repost("m9", 1, rows, 1)
    if deleted_originals:
        await app.db.mark_migration_deleted("m9", 1, "old", [1, 2, 3])
    return text
