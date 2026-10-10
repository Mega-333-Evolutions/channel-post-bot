"""A post Telegram holds back - a copyright strike shows "This message couldn't be displayed on your device due to
copyright infringement" instead of the post: /shift builds the copy from what My posts saved of it, /repost leaves it
alone. The notice itself is never copied."""
import asyncio

from telethon import functions, types

from app.repost_engine import RepostOptions, plan_repost, saved_lookup
from app.shift_engine import run_shift
from app.tgutil import button_url

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, make_msg, markup, photo_media, rpc, service_msg, url_btn
from .harness import norm
from .test_repost_flow import fill, make_app
from .test_restricted import NOTICE, strike
from .test_shift import SRC, old_posts, shift_app
from .test_sync import saved


def run(coro):
    return asyncio.run(coro)


def originals():
    """The posts of the source channel as they were before the strike."""
    return {
        1: make_msg(1, "Welcome", [types.MessageEntityBold(0, 7)]),
        2: make_msg(
            2, "Episode 2 is out", [types.MessageEntityBold(0, 9)],
            markup=markup([url_btn("Download", "https://t.me/goku?start=AAA")]),
        ),
        3: make_msg(3, "Caption with link", [types.MessageEntityTextUrl(13, 4, "https://t.me/srcch/5")], media=photo_media(31)),
        4: service_msg(4),
        5: make_msg(5, "Fifth", reply_to=2),
        6: make_msg(6, "Sixth"),
    }


def held_version(m, flagged=True):
    """What Telegram shows of post `m` after the strike: the notice in place of the post (and, if `flagged`, its own
    restriction mark on the message). Id, album and reply are still there."""
    h = make_msg(
        m.id, NOTICE, out=m.out, grouped_id=m.grouped_id, reply_to=m.reply_to.reply_to_msg_id if m.reply_to else None
    )
    return strike(h) if flagged else h


def live_posts(orig, held, flagged=True):
    return {i: (held_version(m, flagged) if i in held else m) for i, m in orig.items()}


async def save_rows(db, orig, skip=()):
    for i, m in orig.items():
        if i not in skip and not isinstance(m, types.MessageService):
            await saved(db, m)


async def held_setup(tmp_path, *, held=(2, 3, 5), no_row=(), flagged=True, file_available=True, orig=None, last=None):
    """The source (id 1) after the strike, the destination (id 2) and a shift s1 over all of it; My posts has what the
    source's posts looked like before."""
    orig = originals() if orig is None else orig
    src = FakeChannelClient(live_posts(orig, held, flagged), username="srcch", title="Anime Source")
    dst = src.add_channel(FakeChannelClient(old_posts(), username="dstch", cid=2, title="Anime Backup"))
    if file_available:  # Telegram still knows the files: they were not taken down
        for m in orig.values():
            src._remember_media(m)
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Anime Source", "srcch", 1)
    await db.save_channel(2, 6, "Anime Backup", "dstch", 1)
    await save_rows(db, orig, no_row)
    await db.create_shift("s1", src=SRC, dst_channel_id=2, first_id=1, last_id=last or max(orig), user_id=1)
    return src, dst, db, await db.get_channel(2), await db.get_shift("s1")


def forwarded_ids(chan):
    return [i for r in chan.requests if isinstance(r, functions.messages.ForwardMessagesRequest) for i in r.id]


def sent(chan, kind):
    return [r for r in chan.requests if isinstance(r, kind)]


def shown(chan):
    return {i: m.message for i, m in chan.msgs.items() if i > 10}


# ============================================================================================ /shift, engine
def test_a_held_back_post_is_copied_from_my_posts_and_never_as_the_notice(tmp_path):
    async def go():
        for flagged in (True, False):  # Telegram's own mark on the message, or only the notice as its text
            src, dst, db, dst_row, shift = await held_setup(tmp_path / str(flagged), flagged=flagged)
            res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
            assert (res.failed, res.aborted, res.held_back, res.media_lost) == ([], None, [], [])
            assert res.from_saved == 3 and res.copied_units == 5 and res.used_saved
            # copies: 1->11 2->12 3->13 5->14 6->15 (4 is a service message)
            assert shown(dst) == {
                11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth"
            }
            assert not any(NOTICE in (m.message or "") for m in dst.msgs.values())
            # only the posts Telegram shows as they are were copied by Telegram itself
            assert forwarded_ids(dst) == [1, 6]
            # text, formatting and buttons come from My posts
            assert [(e.offset, e.length) for e in dst.msgs[12].entities] == [(0, 9)]
            btn = dst.msgs[12].reply_markup.rows[0].buttons[0]
            assert (btn.text, button_url(btn)) == ("Download", "https://t.me/goku?start=AAA")
            # the photo of the saved post is there, and the link to the post that was copied later points at its copy
            assert dst.msgs[13].media.photo.id == 31
            assert dst.msgs[13].entities[0].url == "https://t.me/dstch/14"
            # a post that answered another answers the copy of it
            assert dst.msgs[14].reply_to.reply_to_msg_id == 12
            # My posts knows the copies as they are
            row = await db.find_by_message(2, 13)
            assert (row.text, row.media_kind) == ("Caption with link", "photo")
            assert (await db.find_by_message(2, 12)).buttons == [[{"t": "Download", "u": "https://t.me/goku?start=AAA"}]]
            # the source is only read
            assert not [r for r in src.requests if type(r).__name__.startswith(("Edit", "Delete", "Send", "Forward"))]
            await db.close()

    run(go())


def test_a_post_that_telegram_shows_as_it_is_still_comes_from_the_channel(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(2,))
        # My posts has an older text for post 1 (somebody edited it and the sync did not run yet): the channel wins
        row = await db.find_by_message(1, 1)
        await db.update_post(row.id, text="an older text")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.from_saved == 1 and res.held_back == []
        assert dst.msgs[11].message == "Welcome" and dst.msgs[12].message == "Episode 2 is out"
        assert forwarded_ids(dst) == [1, 3, 6]  # post 5 answers a post, so it is posted again; 2 comes from My posts
        await db.close()

    run(go())


def test_a_post_with_telegrams_mark_that_is_still_handed_out_as_it_is_is_an_ordinary_post(tmp_path):
    async def go():
        orig = originals()
        live = dict(orig)
        live[2] = strike(make_msg(2, "Episode 2 is out", [types.MessageEntityBold(0, 9)], markup=orig[2].reply_markup))
        live[3] = strike(make_msg(3, "Caption with link", [types.MessageEntityTextUrl(13, 4, "https://t.me/srcch/5")], media=photo_media(31)))
        src = FakeChannelClient(live, username="srcch", title="Anime Source")
        dst = src.add_channel(FakeChannelClient(old_posts(), username="dstch", cid=2, title="Anime Backup"))
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Anime Source", "srcch", 1)
        await db.save_channel(2, 6, "Anime Backup", "dstch", 1)
        await save_rows(db, orig, skip=(2,))  # no saved copy of post 2; post 3 has one
        await db.create_shift("s1", src=SRC, dst_channel_id=2, first_id=1, last_id=6, user_id=1)
        res = await run_shift(src, db, await db.get_shift("s1"), await db.get_channel(2), 99, delay=0, settle_pause=0)
        # the live posts are what they are: nothing is rebuilt from My posts, nothing is skipped
        assert res.from_saved == 0 and res.held_back == [] and res.failed == [] and res.copied_units == 5
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth"}
        assert 3 in forwarded_ids(dst)
        await db.close()

    run(go())


def test_a_post_that_telegram_empties_out_is_held_back_too(tmp_path):
    async def go():
        orig = originals()
        live = dict(orig)
        live[2] = strike(make_msg(2, ""), "Removed after a copyright complaint")  # nothing of the post is handed out
        src = FakeChannelClient(live, username="srcch", title="Anime Source")
        dst = src.add_channel(FakeChannelClient(old_posts(), username="dstch", cid=2, title="Anime Backup"))
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Anime Source", "srcch", 1)
        await db.save_channel(2, 6, "Anime Backup", "dstch", 1)
        await save_rows(db, orig)
        await db.create_shift("s1", src=SRC, dst_channel_id=2, first_id=1, last_id=6, user_id=1)
        res = await run_shift(src, db, await db.get_shift("s1"), await db.get_channel(2), 99, delay=0, settle_pause=0)
        assert res.from_saved == 1 and res.held_back == [] and res.failed == []
        assert dst.msgs[12].message == "Episode 2 is out" and res.held_why == "Removed after a copyright complaint"
        await db.close()

    run(go())


def test_a_text_that_my_posts_has_too_is_the_owners_own_wording_not_a_notice(tmp_path):
    async def go():
        orig = originals()
        orig[6] = make_msg(6, NOTICE)  # the owner really wrote this
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(2, 3, 5), orig=orig, flagged=False)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.from_saved == 3 and res.held_back == [] and res.failed == []
        assert dst.msgs[15].message == NOTICE and 6 in forwarded_ids(dst)  # copied as it is
        await db.close()

    run(go())


def test_a_held_back_post_without_a_saved_copy_is_skipped_and_reported(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, no_row=(3,))
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.held_back == [3] and res.from_saved == 2 and res.failed == [] and res.aborted is None
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "Fifth", 14: "Sixth"}
        assert not any(NOTICE in (m.message or "") for m in dst.msgs.values())
        assert res.held_why == NOTICE
        # once My posts has it (a backup put back with /import) the rest can be done
        await save_rows(db, {3: originals()[3]}, skip=())
        again = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert again.held_back == [] and again.from_saved == 1 and again.copied_units == 1 and again.skipped_done == 4
        assert dst.msgs[15].message == "Caption with link" and dst.msgs[15].media.photo.id == 31
        await db.close()

    run(go())


def test_a_channel_that_is_not_in_my_posts_at_all_has_nothing_to_copy_held_back_posts_from(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(1, 2, 3, 5, 6), no_row=(1, 2, 3, 5, 6))
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.held_back == [1, 2, 3, 5, 6] and res.copied_units == 0 and res.failed == []
        assert shown(dst) == {}
        await db.close()

    run(go())


def test_a_post_with_nothing_saved_in_it_is_not_posted_empty(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(6,), no_row=(6,))
        await db.create_post(
            channel_id=1, message_id=6, status="sent", source="adopted", created_by=1, link_preview=False,
            text="  ", entities=[], buttons=[], media_kind=None, media_file_id=None,
        )
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.held_back == [6] and 15 not in dst.msgs
        await db.close()

    run(go())


# ------------------------------------------------------------------------------------------------ the media
def test_a_file_reference_that_has_expired_is_tried_again_without_it(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path)
        original = dst._do_SendMediaRequest
        tries = []

        def picky(req):
            tries.append(req.media.id.file_reference)
            if req.media.id.file_reference:  # the saved reference is old
                raise rpc("FileReferenceExpiredError")
            return original(req)

        dst._do_SendMediaRequest = picky
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert tries == [b"ref31", b""] and res.media_lost == [] and res.failed == []
        assert dst.msgs[13].media.photo.id == 31
        await db.close()

    run(go())


def test_a_file_telegram_refuses_goes_out_without_it_and_is_reported(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, file_available=False)  # taken down
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.media_lost == [3] and res.failed == [] and res.from_saved == 3 and res.aborted is None
        assert dst.msgs[13].message == "Caption with link" and dst.msgs[13].media is None
        assert len(sent(dst, functions.messages.SendMediaRequest)) == 2  # as saved, then without the file reference
        # the entry in My posts says what the post really is
        row = await db.find_by_message(2, 13)
        assert (row.text, row.media_kind, row.media_file_id) == ("Caption with link", None, None)
        await db.close()

    run(go())


def test_a_post_that_is_only_a_refused_file_fails_and_the_others_are_copied(tmp_path):
    async def go():
        orig = originals()
        orig[7] = make_msg(7, "", media=photo_media(71))
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(7,), orig=orig, file_available=False, last=7)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.failed == [(7, "MediaEmptyError")] and res.media_lost == []
        assert res.copied_units == 5 and 16 not in dst.msgs  # 1 2 3 5 6 were copied; 7 has no text to go out with
        await db.close()

    run(go())


def test_a_held_back_album_is_rebuilt_as_an_album(tmp_path):
    async def go():
        orig = originals()
        orig[8] = make_msg(8, "", media=photo_media(81), grouped_id=80)
        orig[9] = make_msg(9, "album caption", [types.MessageEntityBold(0, 5)], media=photo_media(82), grouped_id=80)
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(8, 9), orig=orig, last=9)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.from_saved == 1 and res.held_back == [] and res.failed == []  # an album is one post
        album = [dst.msgs[i] for i in (16, 17)]
        assert album[0].grouped_id and album[0].grouped_id == album[1].grouped_id
        assert [m.media.photo.id for m in album] == [81, 82]
        assert album[1].message == "album caption" and [(e.offset, e.length) for e in album[1].entities] == [(0, 5)]
        await db.close()

    run(go())


def test_an_album_of_which_one_picture_has_no_saved_copy_is_skipped_whole(tmp_path):
    async def go():
        orig = originals()
        orig[8] = make_msg(8, "", media=photo_media(81), grouped_id=80)
        orig[9] = make_msg(9, "album caption", media=photo_media(82), grouped_id=80)
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(8, 9), no_row=(9,), orig=orig, last=9)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.held_back == [8] and 16 not in dst.msgs
        await db.close()

    run(go())


# ------------------------------------------------------------------------------------------------ the plan
def test_the_plan_counts_what_is_copied_from_my_posts_and_what_is_skipped(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, no_row=(5,))
        lookup = saved_lookup(db, 1)
        plan = await plan_repost(src, SRC, RepostOptions(), saved=lookup, use_saved=True)
        # posts 1 2 3 6 are copied (2 and 3 from My posts); 5 is held back without a saved copy; 4 is a service message
        assert (plan.units, plan.held_saved, plan.held_skipped, plan.service) == (4, 2, 1, 1)
        assert plan.held_why == NOTICE and plan.channel_restricted is None
        assert plan.media == 1 and plan.n_buttons == 0 and plan.error is None  # counted from the saved posts
        # as /repost sees it: nothing held back is copied
        plan = await plan_repost(src, SRC, RepostOptions(), saved=lookup)
        assert (plan.units, plan.held_saved, plan.held_skipped) == (2, 0, 3)
        await db.close()

    run(go())


def test_the_plan_says_so_when_the_whole_channel_is_restricted_and_nothing_can_be_copied(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(1, 2, 3, 5, 6), no_row=(1, 2, 3, 5, 6))
        src.restriction = NOTICE
        plan = await plan_repost(src, SRC, RepostOptions(), saved=saved_lookup(db, 1), use_saved=True)
        assert plan.channel_restricted == NOTICE and plan.units == 0 and plan.held_skipped == 5
        assert "holds back every post" in plan.error and "no saved copy" in plan.error
        await db.close()

    run(go())


def test_a_normal_channel_is_planned_as_before(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=())
        plan = await plan_repost(src, SRC, RepostOptions(), saved=saved_lookup(db, 1), use_saved=True)
        assert (plan.units, plan.held_saved, plan.held_skipped, plan.held_why, plan.channel_restricted) == (5, 0, 0, None, None)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.from_saved == 0 and res.held_back == [] and res.copied_units == 5
        await db.close()

    run(go())


# ============================================================================================ the owner's view
async def held_app(tmp_path, *, held=(2, 3, 5), no_row=(), file_available=True):
    app, dst = await shift_app(tmp_path)
    orig = originals()
    app.tg._media.clear()  # the account knows no file yet (shift_app had filled the channel with other posts)
    fill(app.tg, live_posts(orig, held))
    if file_available:
        for m in orig.values():
            app.tg._remember_media(m)
    await save_rows(app.db, orig, no_row)
    return app, dst


def buttons(app):
    return [t for row in app.out.last_buttons() for t in row]


def test_the_shift_plan_and_result_explain_what_comes_from_my_posts(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, no_row=(5,))
        app.tg.restriction = NOTICE
        await app.text("/shift @srcch @dstch")
        plan = norm(app.out.last_text)
        assert "Telegram restricts Anime Source itself" in plan
        assert "Telegram holds back 2 post(s) of Anime Source" in plan and "copied from what My posts saved of them" in plan
        assert "holds back 1 more post(s)" in plan and "no saved copy" in plan and "/import" in plan
        assert "4 posts" in plan  # 1, 2, 3 and 6 are copied
        assert not any(isinstance(r, functions.messages.SendMessageRequest) for r in dst.requests)  # a plan posts nothing

        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "2 post(s) are held back by Telegram" in done and "copied from what My posts saved of them" in done
        assert "1 post(s) are held back by Telegram and My posts has no saved copy of them" in done and "#5" in done
        assert "not everything was copied" in done  # so the rest can be tried again later
        assert any("Try the rest again" in t for t in buttons(app))
        assert not any(NOTICE in (m.message or "") for m in dst.msgs.values())

        # the missing copy arrives (a backup put back with /import): the rest can be copied
        await save_rows(app.db, {5: originals()[5]})
        await app.press(app.out.callback_data("Try the rest again"))
        done = norm(app.out.last_text)
        assert "message(s) copied" in done and "Undo" in " ".join(buttons(app))
        assert dst.msgs[15].message == "Fifth"
        await app.db.close()

    run(go())


def test_a_refused_file_is_named_in_the_result(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, file_available=False)
        await app.text("/shift @srcch @dstch")
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "1 of them went out without their media" in done and "#3" in done
        await app.db.close()

    run(go())


def test_the_usage_tells_about_posts_telegram_holds_back(tmp_path):
    async def go():
        app, _ = await shift_app(tmp_path)
        await app.text("/shift")
        text = norm(app.out.last_text)
        assert "copyright infringement" in text and "copied from what My posts saved of it" in text
        await app.db.close()

    run(go())


# ============================================================================================ /repost
def repost_posts():
    return {
        1: make_msg(1, "First post"),
        2: make_msg(2, "Second post", markup=markup([url_btn("Watch", "https://t.me/goku?start=BBB")])),
        3: make_msg(3, "Third post"),
    }


def test_a_repost_never_copies_the_notice_and_never_deletes_what_it_could_not_copy(tmp_path):
    async def go():
        orig = repost_posts()
        app = await make_app(tmp_path, msgs=live_posts(orig, (2,)))
        await save_rows(app.db, orig)
        await app.text("/repost --no-typed")
        await app.press(app.out.callback_data("Test"))
        plan = norm(app.out.last_text)
        assert "Telegram holds back 1 post(s) of Test" in plan and "Copying the notice would be wrong" in plan
        assert "2 posts" in plan  # posts 1 and 3
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "1 post(s) are held back by Telegram" in done and "#2" in done and "the old posts are not deleted" in done
        assert "not everything was copied" in done
        assert not any("Delete the old posts" in t for t in buttons(app))  # nothing may be deleted
        # the channel: the three posts, the held back one stays as it is, two copies at the end - and no notice copy
        texts = [m.message for _, m in sorted(app.tg.msgs.items())]
        assert texts == ["First post", NOTICE, "Third post", "First post", "Third post"]
        await app.db.close()

    run(go())


def test_a_repost_of_a_channel_without_held_back_posts_is_unchanged(tmp_path):
    async def go():
        app = await make_app(tmp_path, msgs=repost_posts())
        await app.text("/repost --no-typed")
        await app.press(app.out.callback_data("Test"))
        assert "holds back" not in norm(app.out.last_text)
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "all posts copied in order" in done and "held back" not in done
        assert any("Delete the old posts" in t for t in buttons(app))
        await app.db.close()

    run(go())


# ============================================================================ a source that can't be read at all
def unreadable(chan):
    """The channel is banned or private for the bot: nothing in it can be read any more."""
    chan.fail["GetMessagesRequest"] = rpc("ChannelPrivateError")
    chan.fail["GetFullChannelRequest"] = rpc("ChannelPrivateError")


def test_a_source_that_cannot_be_read_is_copied_from_my_posts_alone(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=())
        unreadable(src)
        await db.set_mark("s1", "fromdb")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.from_db and res.used_saved and res.failed == [] and res.aborted is None and res.held_back == []
        assert res.from_saved == 5 and res.copied_units == 5
        # copies: 1->11 2->12 3->13 5->14 6->15 (there is nothing saved of the service message 4)
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth"}
        assert forwarded_ids(dst) == []  # nothing is copied by Telegram: the source can't be read
        btn = dst.msgs[12].reply_markup.rows[0].buttons[0]
        assert (btn.text, button_url(btn)) == ("Download", "https://t.me/goku?start=AAA")
        assert dst.msgs[13].media.photo.id == 31
        assert dst.msgs[13].entities[0].url == "https://t.me/dstch/14"  # the link to post 5 follows its copy
        assert dst.msgs[14].reply_to is None  # My posts does not know what a post answered
        assert (await db.find_by_message(2, 13)).media_kind == "photo"
        await db.close()

    run(go())


def test_the_range_is_respected_and_a_stopped_shift_goes_on_from_my_posts(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=())
        unreadable(src)
        await db.set_mark("s1", "fromdb")
        calls = []

        async def prog(r):
            calls.append(r.copied_units)

        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0, progress=prog, should_stop=lambda: len(calls) >= 2)
        assert res.stopped and res.copied_units == 2 and shown(dst) == {11: "Welcome", 12: "Episode 2 is out"}
        again = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert again.from_db and not again.stopped and again.copied_units == 3 and again.skipped_done == 2
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth"}
        await db.close()

    run(go())


def test_a_post_with_nothing_to_post_is_not_taken_from_my_posts(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), no_row=(2,))
        await db.create_post(
            channel_id=1, message_id=2, status="sent", source="adopted", created_by=1, link_preview=False,
            text="", entities=[], buttons=[], media_kind=None, media_file_id=None,
        )
        unreadable(src)
        await db.set_mark("s1", "fromdb")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.copied_units == 4 and res.failed == []
        assert shown(dst) == {11: "Welcome", 12: "Caption with link", 13: "Fifth", 14: "Sixth"}
        await db.close()

    run(go())


def test_the_shift_command_offers_my_posts_when_the_source_can_not_be_read(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, held=())
        unreadable(app.tg)
        await app.text("/shift @srcch @dstch")
        plan = norm(app.out.last_text)
        assert "I can't read Anime Source" in plan and "My posts has the saved posts of it" in plan
        assert "5 posts" in plan and "Post ids 1-6" in plan
        assert "replies and albums" in plan and "from what My posts saved of it" in plan
        assert dst.msgs.keys() == set(range(1, 11))  # a plan posts nothing

        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "5 message(s) copied" in done and "The source can't be read, so 5 post(s) were copied" in done
        sid = app.out.callback_data("Undo").split(":")[1]
        assert await app.db.marks_of(sid) == {"fromdb"}
        assert dst.msgs[12].message == "Episode 2 is out"

        await app.press(app.out.callback_data("Undo"))
        await app.press(app.out.callback_data("Yes, remove"))
        assert "5 copied message(s) removed" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_the_range_of_a_shift_from_my_posts(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, held=())
        unreadable(app.tg)
        await app.text("/shift @srcch @dstch 2 3")
        plan = norm(app.out.last_text)
        assert "Post ids 2-3" in plan and "2 posts" in plan
        await app.press(app.out.callback_data("Start copying"))
        assert [m.message for i, m in sorted(dst.msgs.items()) if i > 10] == ["Episode 2 is out", "Caption with link"]
        await app.db.close()

    run(go())


def test_an_unreadable_source_that_my_posts_does_not_know_is_still_an_error(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)  # My posts has nothing of the source
        unreadable(app.tg)
        await app.text("/shift @srcch @dstch")
        text = norm(app.out.last_text)
        assert "I can't read Anime Source" in text and "no saved posts of it" in text
        assert not any(isinstance(r, functions.messages.SendMessageRequest) for r in dst.requests)
        await app.db.close()

    run(go())
