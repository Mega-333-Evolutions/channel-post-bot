"""A source channel that TELEGRAM restricts (a copyright strike: "This channel is unavailable due to copyright
infringement"): /shift copies it from what My posts saved - every post that has a saved copy, whatever Telegram hands out
of it now - and never copies Telegram's notice. Every copy is read back, so the report can say when Telegram itself hides
the NEW posts (a blocked file, a restricted destination) instead of the bot being blamed for copying a notice."""
import asyncio

from telethon import functions

from app.repost_engine import CHECK_FIRST, RepostOptions, plan_repost, saved_lookup
from app.restrictions import message_restriction
from app.shift_engine import run_shift
from app.tgutil import button_url

from .fakes import make_msg, rpc
from .harness import norm
from .test_held_back import (
    buttons,
    held_app,
    held_setup,
    held_version,
    forwarded_ids,
    originals,
    shown,
    unreadable,
)
from .test_restricted import CHANNEL_NOTICE, NOTICE
from .test_shift import SRC


def run(coro):
    return asyncio.run(coro)


COPIES = {11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth"}  # as My posts has them


def differ(src, ids):
    """What Telegram hands out of these posts now is not what My posts saved (so the source of a copy can be told)."""
    for i in ids:
        old = src.msgs[i]
        src.msgs[i] = make_msg(i, f"LIVE {i}", reply_to=old.reply_to.reply_to_msg_id if old.reply_to else None)


async def struck(tmp_path, *, no_row=(), live_differs=True, **kw):
    """The source is restricted as a whole; its posts are handed out intact (and, by default, differently from what My
    posts saved) and carry no mark of their own - the case that went wrong."""
    src, dst, db, dst_row, shift = await held_setup(tmp_path, held=kw.pop("held", ()), no_row=no_row, **kw)
    src.restriction = CHANNEL_NOTICE
    if live_differs:
        differ(src, [i for i in (1, 2, 3, 5, 6) if i in src.msgs and not message_restriction(src.msgs[i])])
    return src, dst, db, dst_row, shift


# ======================================================================================== the copy loop
def test_a_struck_channel_is_copied_from_my_posts_even_when_telegram_still_hands_the_posts_out(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path)
        dst.fail["ForwardMessagesRequest"] = rpc("MessageIdInvalidError")  # what the real run met: nothing can be forwarded
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.failed, res.aborted, res.held_back) == ([], None, [])
        assert res.from_saved == 5 and res.copied_units == 5 and res.live_restricted == 0 and res.held_why == CHANNEL_NOTICE
        assert shown(dst) == COPIES  # My posts' texts - not "LIVE n"
        assert forwarded_ids(dst) == []  # Telegram is not even asked to copy anything out of the struck channel
        # text, formatting, buttons, media and links come from My posts
        assert [(e.offset, e.length) for e in dst.msgs[12].entities] == [(0, 9)]
        btn = dst.msgs[12].reply_markup.rows[0].buttons[0]
        assert (btn.text, button_url(btn)) == ("Download", "https://t.me/goku?start=AAA")
        assert dst.msgs[13].media.photo.id == 31
        assert dst.msgs[13].entities[0].url == "https://t.me/dstch/14"  # the link to post 5 follows its copy
        assert dst.msgs[14].reply_to.reply_to_msg_id == 12  # an answer answers the copy
        # My posts knows the copies as they are, and the source was only read
        assert (await db.find_by_message(2, 12)).text == "Episode 2 is out"
        assert not [r for r in src.requests if type(r).__name__.startswith(("Edit", "Delete", "Send", "Forward"))]
        await db.close()

    run(go())


def test_a_struck_channel_is_copied_from_my_posts_when_telegram_hands_out_only_the_notice(tmp_path):
    """The same channel when the apps show the notice: the posts carry Telegram's mark, or only its words as the text."""

    async def go():
        for flagged in (True, False):
            src, dst, db, dst_row, shift = await struck(tmp_path / str(flagged), held=(1, 2, 3, 5, 6), flagged=flagged)
            res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
            assert (res.failed, res.aborted, res.held_back) == ([], None, [])
            assert res.from_saved == 5 and shown(dst) == COPIES
            assert not any(NOTICE in (m.message or "") for m in dst.msgs.values())
            await db.close()

    run(go())


def test_a_post_of_a_struck_channel_without_a_saved_copy_is_copied_as_telegram_hands_it_out(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path, no_row=(3, 5))
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.failed, res.aborted, res.held_back) == ([], None, [])
        assert res.from_saved == 3 and res.live_restricted == 2  # 1 2 6 from My posts; 3 and 5 as they are now
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "LIVE 3", 14: "LIVE 5", 15: "Sixth"}
        assert forwarded_ids(dst) == []  # not even those: a restricted post is never copied by Telegram itself
        await db.close()

    run(go())


def test_a_post_of_a_struck_channel_with_only_the_notice_and_no_saved_copy_is_skipped(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path, held=(3,), no_row=(3,), flagged=False)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.held_back == [3] and res.from_saved == 4 and res.failed == []
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "Fifth", 14: "Sixth"}
        assert not any(NOTICE in (m.message or "") for m in dst.msgs.values())
        await db.close()

    run(go())


def test_a_saved_copy_that_is_telegrams_notice_is_never_posted(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path, no_row=(2, 3))
        for mid in (2, 3):  # what an older version saved when it synced the struck channel
            await db.create_post(
                channel_id=1, message_id=mid, status="sent", source="adopted", created_by=1, link_preview=False,
                text=NOTICE, entities=[], buttons=[], media_kind=None, media_file_id=None,
            )
        src.msgs[3] = held_version(originals()[3], flagged=False)  # post 3: the notice from Telegram as well
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        # post 2: the saved copy is useless but Telegram hands the post out; post 3: nothing but notices
        assert res.held_back == [3] and res.live_restricted == 1 and res.from_saved == 3 and res.failed == []
        assert shown(dst) == {11: "Welcome", 12: "LIVE 2", 13: "Fifth", 14: "Sixth"}
        assert not any(NOTICE in (m.message or "") for m in dst.msgs.values())
        await db.close()

    run(go())


def test_a_source_that_cannot_be_read_never_gets_a_saved_notice_posted(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), no_row=(2,))
        await db.create_post(
            channel_id=1, message_id=2, status="sent", source="adopted", created_by=1, link_preview=False,
            text=NOTICE, entities=[], buttons=[], media_kind=None, media_file_id=None,
        )
        unreadable(src)
        await db.set_mark("s1", "fromdb")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.copied_units == 4 and res.failed == []
        assert shown(dst) == {11: "Welcome", 12: "Caption with link", 13: "Fifth", 14: "Sixth"}
        await db.close()

    run(go())


def test_a_channel_nothing_restricts_is_copied_as_before(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=())
        differ(src, (1, 2, 3, 5, 6))
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.from_saved == 0 and res.live_restricted == 0 and res.held_why is None and res.held_back == []
        assert shown(dst) == {11: "LIVE 1", 12: "LIVE 2", 13: "LIVE 3", 14: "LIVE 5", 15: "LIVE 6"}  # what the channel says
        await db.close()

    run(go())


def test_lifting_the_strike_makes_the_next_run_an_ordinary_one(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path, last=3)
        stopper = {"n": 0}

        async def prog(r):
            stopper["n"] = r.copied_units

        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0, progress=prog, should_stop=lambda: stopper["n"] >= 1)
        assert res.stopped and shown(dst) == {11: "Welcome"}  # post 1, from My posts
        src.restriction = None  # the strike is lifted: the channel says what it says now
        again = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert again.held_why is None and again.from_saved == 0
        assert shown(dst) == {11: "Welcome", 12: "LIVE 2", 13: "LIVE 3"}
        await db.close()

    run(go())


# ================================================================================================ the plan
def test_the_plan_counts_a_struck_channel_as_copied_from_my_posts(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path, no_row=(5,))
        lookup = saved_lookup(db, 1)
        plan = await plan_repost(src, SRC, RepostOptions(), saved=lookup, use_saved=True)
        assert plan.channel_restricted == CHANNEL_NOTICE and plan.held_why == CHANNEL_NOTICE
        # 1 2 3 6 have a saved copy; 5 has none but Telegram still hands it out
        assert (plan.units, plan.held_saved, plan.live_restricted, plan.held_skipped, plan.service) == (5, 4, 1, 0, 1)
        assert plan.media == 1  # post 3 is counted from its saved copy, not from what Telegram hands out now
        # /repost copies the posts of the channel itself, post by post: a struck channel changes nothing about that
        plain = await plan_repost(src, SRC, RepostOptions(), saved=lookup)
        assert (plain.units, plain.held_saved, plain.live_restricted, plain.held_skipped) == (5, 0, 0, 0)
        assert plain.channel_restricted == CHANNEL_NOTICE
        await db.close()

    run(go())


# ========================================================================================= the read-back
def test_every_copy_of_a_struck_channel_is_read_back_and_what_telegram_hides_is_counted(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path)
        dst.restrict_new = lambda m: CHANNEL_NOTICE if m.media is not None else None  # the file of post 3 is blocked
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.copied_units == 5 and res.failed == [] and res.held_back == []
        assert res.checked == 5  # every copy of a restricted source
        assert res.hidden == [(3, True)] and res.hidden_why == CHANNEL_NOTICE and res.dst_restricted is None
        # the bot posted the saved text; the notice is Telegram's mark on the new post
        assert dst.msgs[13].message == "Caption with link" and message_restriction(dst.msgs[13]) == CHANNEL_NOTICE
        assert not any(NOTICE in (m.message or "") for m in dst.msgs.values())
        await db.close()

    run(go())


def test_a_destination_that_is_restricted_itself_is_named(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path)
        dst.restriction = CHANNEL_NOTICE
        dst.restrict_new = lambda m: CHANNEL_NOTICE
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert [i for i, _ in res.hidden] == [1, 2, 3] and res.dst_restricted == CHANNEL_NOTICE  # it stopped after 3
        assert res.hidden_stop
        await db.close()

    run(go())


def test_a_normal_channel_is_only_sampled_when_the_copies_are_read_back(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=())
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.copied_units == 5 and res.checked == CHECK_FIRST and res.hidden == [] and res.dst_restricted is None
        assert not [r for r in dst.requests if isinstance(r, functions.channels.GetFullChannelRequest)]  # nothing to ask
        await db.close()

    run(go())


def test_a_read_back_that_fails_does_not_fail_the_copy(tmp_path):
    async def go():
        for error in (rpc("ChannelPrivateError"), ConnectionError("the network dropped")):  # Telegram's refusal, or not
            src, dst, db, dst_row, shift = await struck(tmp_path / type(error).__name__, last=1)  # post 1: no buttons to read back
            dst.fail["GetMessagesRequest"] = error
            res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
            assert res.copied_units == 1 and res.failed == [] and res.checked == 0 and res.hidden == []
            assert shown(dst) == {11: "Welcome"}
            await db.close()

    run(go())


def test_a_source_that_cannot_be_asked_about_its_restriction_is_copied_as_an_unrestricted_one(tmp_path):
    async def go():
        for error in (rpc("ChannelPrivateError"), ConnectionError("the network dropped")):
            src, dst, db, dst_row, shift = await held_setup(tmp_path / type(error).__name__, held=())
            src.fail["GetFullChannelRequest"] = error
            res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
            assert res.copied_units == 5 and res.failed == [] and res.held_why is None and res.from_saved == 0
            await db.close()

    run(go())


# ================================================================================================ the owner's view
def test_the_plan_and_the_result_for_a_struck_channel_read_as_they_should(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, held=())
        app.tg.restriction = CHANNEL_NOTICE
        dst.fail["ForwardMessagesRequest"] = rpc("MessageIdInvalidError")
        await app.text("/shift @srcch @dstch")
        plan = norm(app.out.last_text)
        assert "Telegram restricts Anime Source itself" in plan
        assert "5 post(s) of Anime Source are restricted by Telegram" in plan and "copied from what My posts saved" in plan
        assert "5 posts" in plan and "The new posts are read back" in plan

        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "5 post(s) are restricted by Telegram" in done and "copied from what My posts saved of them" in done
        assert "5 message(s) copied" in done and "hides" not in done
        assert [m.message for i, m in sorted(dst.msgs.items()) if i > 10] == list(COPIES.values())
        await app.db.close()

    run(go())


def test_the_report_says_when_telegram_itself_hides_the_new_posts(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, held=())
        app.tg.restriction = CHANNEL_NOTICE
        dst.restrict_new = lambda m: CHANNEL_NOTICE if m.media is not None else None
        await app.text("/shift @srcch @dstch")
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "Telegram itself hides 1 of the 5 new post(s) I read back" in done
        assert "carry its restriction mark" in done and "never posts one" in done
        assert "1 post(s) of these carry a file" in done and "--no-media" in done
        assert "The saved posts in My posts are intact" in done
        assert "5 message(s) copied" in done  # the copies exist: Undo removes them
        assert any("Undo" in t for row in app.out.last_buttons() for t in row)
        await app.db.close()

    run(go())


def test_the_report_names_a_restricted_destination(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, held=())
        app.tg.restriction = CHANNEL_NOTICE
        dst.restriction = CHANNEL_NOTICE
        dst.restrict_new = lambda m: CHANNEL_NOTICE
        await app.text("/shift @srcch @dstch")
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "Telegram itself hides 3 of the 3 new post(s) I read back" in done
        assert "Telegram restricts the destination channel itself" in done
        await app.db.close()

    run(go())


def test_a_repost_plan_for_a_struck_channel_points_at_shift(tmp_path):
    from .test_repost_flow import make_app

    async def go():
        app = await make_app(tmp_path, msgs={1: make_msg(1, "First post"), 2: make_msg(2, "Second post")})
        app.tg.restriction = CHANNEL_NOTICE
        await app.text("/repost --no-typed")
        await app.press(app.out.callback_data("Test"))
        plan = norm(app.out.last_text)
        assert "Telegram restricts Test itself" in plan and "use /shift" in plan
        assert "2 posts" in plan and "no saved copy" not in plan  # /repost does not look at My posts
        await app.db.close()

    run(go())


# ====================================================================== stopping when Telegram hides the copies
def test_a_shift_stops_by_itself_when_telegram_hides_every_new_copy(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path)
        dst.restrict_new = lambda m: CHANNEL_NOTICE  # every new post is hidden
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.stopped and res.hidden_stop and res.copied_units == 3  # not 5: it did not go on
        assert [i for i, _ in res.hidden] == [1, 2, 3] and res.checked == 3
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link"}
        assert res.phase == "copy" and res.final is None  # the finishing touches wait for the rest
        await db.close()

    run(go())


def test_the_owner_can_go_on_anyway_and_it_does_not_stop_again(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path)
        dst.restrict_new = lambda m: CHANNEL_NOTICE
        first = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert first.hidden_stop and first.copied_units == 3
        await db.set_mark("s1", "hiddenok")  # what pressing Continue anyway rests on
        again = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert not again.stopped and not again.hidden_stop and again.copied_units == 2 and again.skipped_done == 3
        assert shown(dst) == COPIES
        await db.close()

    run(go())


def test_a_shift_does_not_stop_when_only_some_copies_are_hidden(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path)
        dst.restrict_new = lambda m: CHANNEL_NOTICE if m.media is not None else None  # only the photo
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert not res.stopped and res.copied_units == 5 and res.hidden == [(3, True)]
        await db.close()

    run(go())


def test_a_shift_with_fewer_copies_than_the_threshold_is_not_stopped(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await struck(tmp_path, last=2)
        dst.restrict_new = lambda m: CHANNEL_NOTICE
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert not res.stopped and res.copied_units == 2 and len(res.hidden) == 2  # reported, nothing left to stop
        await db.close()

    run(go())


def test_the_stopped_shift_explains_itself_and_continue_anyway_goes_on(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, held=())
        app.tg.restriction = CHANNEL_NOTICE
        dst.restrict_new = lambda m: CHANNEL_NOTICE
        await app.text("/shift @srcch @dstch")
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "stopped" in done and "3 message(s) copied so far" in done
        assert "I stopped after 3 post(s)" in done and "Telegram hides every new copy I read back" in done
        assert "Telegram itself hides 3 of the 3 new post(s) I read back" in done
        assert any("Continue" in t for t in buttons(app)) and any("Undo" in t for t in buttons(app))
        sid = app.out.callback_data("Undo").split(":")[1]
        assert await app.db.marks_of(sid) == {"hiddenok"}
        assert len([i for i in dst.msgs if i > 10]) == 3  # three posts, not the whole channel

        await app.press(app.out.callback_data("Continue"))
        done = norm(app.out.last_text)
        assert "5 message(s) copied" in done and "I stopped" not in done  # it went on, as the owner chose
        assert len([i for i in dst.msgs if i > 10]) == 5
        await app.db.close()

    run(go())
