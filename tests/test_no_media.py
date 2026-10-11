"""/shift --no-media: no file is copied - every post goes out as its text with its formatting and buttons. For a source
whose files Telegram blocks (a copyright strike), where a post that carries the file is hidden wherever it is posted."""
import asyncio

from telethon import functions, types

from app.handlers.shift import USAGE, parse_shift_args
from app.repost_engine import RepostOptions, plan_repost, saved_lookup, without_files
from app.shift_engine import plan_from_my_posts, run_shift
from app.tgutil import button_url

from .fakes import make_msg, photo_media
from .harness import norm
from .test_held_back import buttons, held_app, held_setup, forwarded_ids, originals, save_rows, shown, unreadable
from .test_restricted import CHANNEL_NOTICE
from .test_shift import SRC


def run(coro):
    return asyncio.run(coro)


def with_files():
    """The source: text posts, a photo with a caption, a post that is only a photo, an album and an answer to the photo."""
    orig = originals()  # 1 text, 2 text + button, 3 photo + caption, 4 service, 5 answers 2, 6 text
    orig[7] = make_msg(7, "", media=photo_media(71))  # only a photo
    orig[8] = make_msg(8, "", media=photo_media(81), grouped_id=80)  # an album: no caption on the first picture ...
    orig[9] = make_msg(9, "album caption", [types.MessageEntityBold(0, 5)], media=photo_media(82), grouped_id=80)
    orig[10] = make_msg(10, "Reply to the photo", reply_to=7)
    return orig


# ============================================================================================== the option
def test_the_option_is_understood_in_every_spelling():
    for raw in (
        "@a @b --no-media",
        "@a @b -no-media",
        "@a @b --nomedia",
        "@a @b -nomedia",
        "@a @b --NO-MEDIA",
        "@a @b --no_media",
        "@a @b —no-media",  # a phone keyboard turns -- into a long dash
        "@a @b –no-media",
        "@a @b -- no-media",
        "--no-media @a @b",
    ):
        a, b, first, last, flags = parse_shift_args(raw)
        assert (a, b, first, last) == ("@a", "@b", None, None) and flags.no_media and not flags.links and not flags.profile, raw
    a, b, first, last, flags = parse_shift_args("@a @b 5 9 -c --no-media -all")
    assert (a, b, first, last) == ("@a", "@b", 5, 9) and flags.links and flags.profile and flags.no_media
    # a channel id is not an option, and nothing is switched on by accident
    a, b, first, last, flags = parse_shift_args("-1001234567890 @b")
    assert (a, b) == ("-1001234567890", "@b") and not flags.no_media
    try:
        parse_shift_args("@a @b --nomediaa")
    except ValueError as e:
        assert "--no-media" in str(e) and "-all" in str(e)
    else:
        raise AssertionError("an unknown option must be refused")


def test_the_usage_describes_the_option():
    assert "--no-media" in USAGE and "no file is copied" in USAGE


def test_without_files_leaves_a_text_post_and_drops_a_post_that_was_only_a_file():
    photo_caption = make_msg(3, "Caption", media=photo_media(31))
    only_photo = make_msg(7, "", media=photo_media(71))
    plain = make_msg(1, "Welcome")
    kept, lost = without_files([plain, photo_caption, only_photo])
    assert [m.id for m in kept] == [1, 3] and lost == 2
    assert kept[1].media is None and photo_caption.media is not None  # the original message is not changed
    assert without_files([only_photo]) == ([], 1)


# ======================================================================================== the copy loop
def test_the_files_stay_out_and_everything_else_is_copied(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), orig=with_files(), last=10)
        await db.set_mark("s1", "nomedia")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.failed, res.aborted, res.held_back) == ([], None, []) and res.no_media
        assert res.copied_units == 7 and res.files_left_out == 2 and res.file_only == [7, 8]
        # copies: 1->11 2->12 3->13 5->14 6->15 9->16 10->17 (4 is a service message, 7 and 8 are only files)
        assert shown(dst) == {
            11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth",
            16: "album caption", 17: "Reply to the photo",
        }
        assert all(m.media is None for i, m in dst.msgs.items() if i > 10)  # no file anywhere
        assert not sent_media(dst)
        # a post without a file is still copied by Telegram itself - the answer to a post that is only a file too: it has
        # no reply to keep any more
        assert forwarded_ids(dst) == [1, 6, 10]
        # text, formatting, buttons and the links between the copies are kept
        assert [(e.offset, e.length) for e in dst.msgs[16].entities] == [(0, 5)]
        btn = dst.msgs[12].reply_markup.rows[0].buttons[0]
        assert (btn.text, button_url(btn)) == ("Download", "https://t.me/goku?start=AAA")
        assert dst.msgs[13].entities[0].url == "https://t.me/dstch/14"
        assert dst.msgs[14].reply_to.reply_to_msg_id == 12
        # the answer to a post that is only a file answers nothing - and does not wait for a copy that never comes
        assert dst.msgs[17].reply_to is None and res.reply_dropped == 1
        # My posts knows the copies as they are: text, no file
        row = await db.find_by_message(2, 13)
        assert (row.text, row.media_kind, row.media_file_id) == ("Caption with link", None, None)
        # the source was only read
        assert not [r for r in src.requests if type(r).__name__.startswith(("Edit", "Delete", "Send", "Forward"))]
        await db.close()

    run(go())


def sent_media(chan):
    return [r for r in chan.requests if isinstance(r, (functions.messages.SendMediaRequest, functions.messages.SendMultiMediaRequest))]


def test_without_the_option_the_files_are_copied_as_before(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), orig=with_files(), last=10)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert not res.no_media and res.files_left_out == 0 and res.file_only == []
        assert dst.msgs[13].media.photo.id == 31
        await db.close()

    run(go())


def test_a_struck_channel_is_copied_from_my_posts_without_files(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(1, 2, 3, 5, 6, 7), orig=with_files(), last=7)
        src.restriction = CHANNEL_NOTICE
        await db.set_mark("s1", "nomedia")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.failed, res.aborted, res.held_back) == ([], None, [])
        assert res.from_saved == 5 and res.files_left_out == 1 and res.file_only == [7]
        assert shown(dst) == {11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth"}
        assert all(m.media is None for i, m in dst.msgs.items() if i > 10) and not sent_media(dst) and not forwarded_ids(dst)
        await db.close()

    run(go())


def test_a_source_that_cannot_be_read_is_copied_from_my_posts_without_files(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), orig=with_files(), last=10)
        unreadable(src)
        await db.set_mark("s1", "fromdb")
        await db.set_mark("s1", "nomedia")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.from_db and res.failed == [] and res.aborted is None
        # My posts has no album: its two pictures are two saved posts - the one with the caption goes out as text
        assert res.files_left_out == 2 and res.file_only == [7, 8]  # 3 and 9 lost their file; 7 and 8 are only files
        assert shown(dst) == {
            11: "Welcome", 12: "Episode 2 is out", 13: "Caption with link", 14: "Fifth", 15: "Sixth",
            16: "album caption", 17: "Reply to the photo",
        }
        assert not sent_media(dst)
        await db.close()

    run(go())


def test_a_media_only_post_is_skipped_but_never_makes_the_shift_incomplete(tmp_path):
    async def go():
        orig = {1: make_msg(1, "Welcome"), 2: make_msg(2, "", media=photo_media(21)), 3: make_msg(3, "Third")}
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), orig=orig, last=3)
        await db.set_mark("s1", "nomedia")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.file_only == [2] and res.held_back == [] and res.failed == [] and res.copied_units == 2
        await db.close()

    run(go())


# ================================================================================================ the plan
def test_the_plan_counts_posts_without_their_files(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), orig=with_files(), last=10)
        lookup = saved_lookup(db, 1)
        plan = await plan_repost(src, SRC, RepostOptions(), saved=lookup, use_saved=True, no_media=True)
        # 1 2 3 5 6 9 10 are copied (3 and 9 without their file); 7 and 8 are only files; 4 is a service message
        assert (plan.units, plan.files_left_out, plan.file_only, plan.service) == (7, 2, 2, 1)
        assert (plan.media, plan.albums, plan.messages, plan.no_media) == (0, 0, 7, True)
        full = await plan_repost(src, SRC, RepostOptions(), saved=lookup, use_saved=True)
        assert (full.units, full.media, full.albums, full.files_left_out, full.file_only) == (8, 4, 1, 0, 0)
        await db.close()

    run(go())


def test_the_plan_from_my_posts_counts_posts_without_their_files(tmp_path):
    async def go():
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), orig=with_files(), last=10)
        ch = await db.get_channel(1)
        plan = await plan_from_my_posts(db, ch, no_media=True)
        assert (plan.units, plan.files_left_out, plan.file_only, plan.media) == (7, 2, 2, 0)
        await db.close()

    run(go())


def test_a_range_of_nothing_but_files_is_an_error_not_an_empty_plan(tmp_path):
    async def go():
        orig = {1: make_msg(1, "Welcome"), 2: make_msg(2, "", media=photo_media(21)), 3: make_msg(3, "", media=photo_media(31))}
        src, dst, db, dst_row, shift = await held_setup(tmp_path, held=(), orig=orig, last=3)
        plan = await plan_repost(src, SRC, RepostOptions(), first_id=2, last_id=3, saved=saved_lookup(db, 1), use_saved=True, no_media=True)
        assert plan.units == 0 and plan.file_only == 2 and "only a file" in plan.error
        await db.close()

    run(go())


# ========================================================================================= the owner's view
def test_the_command_plans_runs_and_reports_a_shift_without_files(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, held=())
        await app.text("/shift @srcch @dstch --no-media")
        plan = norm(app.out.last_text)
        assert "--no-media" in plan and "no file is copied" in plan
        assert "1 post(s) carry a file that stays out" in plan and "5 posts" in plan
        assert not any(isinstance(r, functions.messages.SendMessageRequest) for r in dst.requests)  # a plan posts nothing

        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "5 message(s) copied" in done and "(without files)" in done
        assert "1 post(s) were copied as text only" in done and "their file was left out" in done
        assert all(m.media is None for i, m in dst.msgs.items() if i > 10)
        sid = app.out.callback_data("Undo").split(":")[1]
        assert await app.db.marks_of(sid) == {"nomedia"}
        await app.db.close()

    run(go())


def test_a_shift_that_is_continued_goes_on_without_files(tmp_path):
    async def go():
        app, dst = await held_app(tmp_path, no_row=(5,))
        app.tg.restriction = CHANNEL_NOTICE
        await app.text("/shift @srcch @dstch --no-media")
        await app.press(app.out.callback_data("Start copying"))
        assert any("Try the rest again" in t for t in buttons(app))  # post 5: only the notice, nothing saved
        await save_rows(app.db, {5: make_msg(5, "Fifth", reply_to=2, media=photo_media(51))})  # the saved copy has a photo
        await app.press(app.out.callback_data("Try the rest again"))
        assert dst.msgs[15].message == "Fifth" and dst.msgs[15].media is None  # the mark kept the option
        assert not sent_media(dst)
        await app.db.close()

    run(go())


def test_the_whole_story_telegram_hides_posts_with_files_and_without_them_it_does_not(tmp_path):
    """The case behind the option: the new copies are hidden by Telegram because of their files."""

    async def go():
        for no_media in (False, True):
            app, dst = await held_app(tmp_path / str(no_media), held=())
            app.tg.restriction = CHANNEL_NOTICE
            dst.restrict_new = lambda m: CHANNEL_NOTICE if m.media is not None else None  # the photo is blocked
            await app.text("/shift @srcch @dstch" + (" --no-media" if no_media else ""))
            await app.press(app.out.callback_data("Start copying"))
            done = norm(app.out.last_text)
            if no_media:
                assert "hides" not in done and "(without files)" in done
            else:
                assert "Telegram itself hides 1 of the 5 new post(s) I read back" in done and "--no-media" in done
            await app.db.close()

    run(go())


def test_the_repost_command_does_not_take_the_option(tmp_path):
    async def go():
        from .test_repost_flow import make_app

        app = await make_app(tmp_path, msgs={1: make_msg(1, "First post")})
        await app.text("/repost --no-media")
        assert "Unknown" in norm(app.out.last_text) or "unknown" in norm(app.out.last_text).lower()
        await app.db.close()

    run(go())
