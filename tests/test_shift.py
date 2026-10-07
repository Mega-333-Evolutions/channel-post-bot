"""/shift: posts of one channel copied into another - range, buttons, replies, links between the posts, undo."""
import asyncio
from types import SimpleNamespace

import pytest
from telethon import functions, types

from app.channelref import classify_ref, resolve_channel
from app.common import UserError
from app.handlers.shift import parse_shift_args
from app.repost_engine import RepostOptions, plan_repost
from app.shift_engine import delete_shift_copies, run_shift
from app.tgutil import button_url

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, cb_btn, make_msg, markup, photo_media, service_msg, url_btn
from .harness import App, norm
from .test_repost_flow import fill

SRC = SimpleNamespace(id=1, access_hash=5, title="Anime Source", username="srcch")


def source_posts():
    return {
        1: make_msg(1, "Welcome", [types.MessageEntityBold(0, 7)]),
        2: make_msg(
            2, "Click below",
            markup=markup([url_btn("Download", "https://t.me/goku?start=AAA")], [cb_btn("👍 3", b"r")]),
        ),
        3: make_msg(3, "Caption with link", [types.MessageEntityTextUrl(13, 4, "https://t.me/srcch/5")], media=photo_media(31)),
        4: service_msg(4),
        5: make_msg(5, "Fifth", reply_to=2, pinned=True),
        6: make_msg(6, "", media=photo_media(61), grouped_id=60),
        7: make_msg(7, "album caption", media=photo_media(62), grouped_id=60),
        8: make_msg(8, "See https://t.me/srcch/1 and https://t.me/c/1/2 and https://t.me/elsewhere/9"),
    }


def old_posts():
    """Posts the destination already has, so that the copies get ids that differ from the originals'."""
    return {i: make_msg(i, f"existing {i}", out=True) for i in range(1, 11)}


def two_channels(src_msgs=None, dst_msgs=None):
    src = FakeChannelClient(source_posts() if src_msgs is None else src_msgs, username="srcch", title="Anime Source")
    dst = src.add_channel(FakeChannelClient(old_posts() if dst_msgs is None else dst_msgs, username="dstch", cid=2, title="Anime Backup"))
    return src, dst


async def engine_setup(tmp_path, src, first=1, last=8):
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(2, 6, "Anime Backup", "dstch", 1)
    dst_row = await db.get_channel(2)
    await db.create_shift("s1", src=SRC, dst_channel_id=2, first_id=first, last_id=last, user_id=1)
    return db, dst_row, await db.get_shift("s1")


def reply_of(m):
    return m.reply_to.reply_to_msg_id if m.reply_to else None


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------- the command line
def test_arguments_are_read_as_source_destination_and_an_optional_range():
    assert parse_shift_args("@a @b") == ("@a", "@b", None, None)
    assert parse_shift_args("@a @b 15 20") == ("@a", "@b", 15, 20)
    assert parse_shift_args("@a @b 20 15") == ("@a", "@b", 15, 20)  # either order
    assert parse_shift_args("@a @b 15") == ("@a", "@b", 15, 15)  # one id = just that post
    assert parse_shift_args("https://t.me/+AbCdEfGhIj -1001234567890") == ("https://t.me/+AbCdEfGhIj", "-1001234567890", None, None)
    for bad in ("", "@a", "@a @b x", "@a @b 0", "@a @b 1 2 3", "@a @b -5"):
        with pytest.raises(ValueError):
            parse_shift_args(bad)


def test_every_kind_of_channel_reference_is_told_apart():
    assert classify_ref("@srcch") == ("username", "srcch")
    assert classify_ref("https://t.me/srcch") == ("username", "srcch")
    assert classify_ref("https://t.me/srcch/15") == ("username", "srcch")
    assert classify_ref("-1001234567890") == ("id", 1234567890)
    assert classify_ref("https://t.me/c/1234567890/5") == ("id", 1234567890)
    assert classify_ref("https://t.me/+AbCdEfGhIjKl") == ("invite", "AbCdEfGhIjKl")
    assert classify_ref("t.me/joinchat/AbCdEfGhIjKl") == ("invite", "AbCdEfGhIjKl")
    assert classify_ref("tg://join?invite=AbCdEfGhIjKl") == ("invite", "AbCdEfGhIjKl")
    assert classify_ref("???") == (None, None)


def test_channels_are_found_by_name_id_or_invite_link(tmp_path):
    async def go():
        app = App(tmp_path)
        await app.start()
        src, dst = two_channels()
        app.tg.registry = src.registry
        app.tg.registry[1] = app.tg
        app.tg.username, app.tg.title = "srcch", "Anime Source"
        app.tg.add_channel(dst)
        await app.db.save_channel(1, 5, "Anime Source", "srcch", 1)  # registered
        # registered: found in the database, whatever the spelling
        for text in ("@srcch", "SRCCH", "https://t.me/srcch/12", "1"):
            ref = await resolve_channel(app.ctx, text)
            assert (ref.id, ref.title, ref.registered) == (1, "Anime Source", True), text
        # not registered: asked from Telegram by name, by id (a channel the bot has met) or by invite link
        ref = await resolve_channel(app.ctx, "@dstch")
        assert (ref.id, ref.title, ref.username, ref.registered) == (2, "Anime Backup", "dstch", False)
        assert (await resolve_channel(app.ctx, "2")).id == 2
        dst.invite_hash = "AbCdEfGhIjKl"
        assert (await resolve_channel(app.ctx, "https://t.me/+AbCdEfGhIjKl")).id == 2
        # things that cannot be used say why
        for text, part in (
            ("???", "not a channel I understand"),
            ("@nosuchchannel", "couldn't find a channel"),
            ("99", "don't know a channel with the id 99"),
            ("https://t.me/+ZzZzZzZzZz", "invite link doesn't work"),
        ):
            with pytest.raises(UserError, match=part):
                await resolve_channel(app.ctx, text)
        await app.db.close()

    run(go())


def test_a_group_or_a_user_is_not_a_channel(tmp_path):
    async def go():
        app = App(tmp_path)
        await app.start()
        group = FakeChannelClient({}, username="agroup", cid=3)
        group._channel_obj = lambda: types.Channel(id=3, title="G", photo=types.ChatPhotoEmpty(), date=None, access_hash=1, username="agroup", megagroup=True)
        app.tg.add_channel(group)
        with pytest.raises(UserError, match="group, not a channel"):
            await resolve_channel(app.ctx, "@agroup")
        await app.db.close()

    run(go())


# ------------------------------------------------------------------------------------------ the engine
def test_posts_arrive_in_the_other_channel_with_everything_they_had(tmp_path):
    async def go():
        src, dst = two_channels()
        db, dst_row, shift = await engine_setup(tmp_path, src)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.copied_messages, res.failed, res.aborted) == (6, 7, [], None)
        new = {i: m for i, m in dst.msgs.items() if i > 10}
        assert list(new) == [11, 12, 13, 14, 15, 16, 17]  # same order as the originals

        assert new[11].message == "Welcome" and isinstance(new[11].entities[0], types.MessageEntityBold)
        # the button post: link button kept, the other bot's 👍 button is not copied (it would be dead)
        rows = new[12].reply_markup.rows
        assert len(rows) == 1 and button_url(rows[0].buttons[0]) == "https://t.me/goku?start=AAA" and rows[0].buttons[0].text == "Download"
        assert new[13].media.photo.id == 31
        # a reply answers the COPY of the post it answered; a pin is not copied
        assert reply_of(new[14]) == 12 and not new[14].pinned
        assert new[15].grouped_id and new[15].grouped_id == new[16].grouped_id and new[16].message == "album caption"
        # links to other source posts follow the content: backward ones at once, forward ones afterwards
        assert new[17].message == "See https://t.me/dstch/11 and https://t.me/dstch/12 and https://t.me/elsewhere/9"
        link = [e for e in new[13].entities if isinstance(e, types.MessageEntityTextUrl)][0]
        assert link.url == "https://t.me/dstch/14" and (link.offset, link.length) == (13, 4)
        assert (res.final.relinked, res.final.links) == (1, 1)

        # the source was only read
        assert sorted(src.msgs) == [1, 2, 3, 4, 5, 6, 7, 8] and src.msgs[5].pinned
        assert src.delete_requests == [] and src.edits == []
        assert not any(isinstance(r, (functions.messages.SendMessageRequest, functions.messages.SendMediaRequest)) for r in src.requests)

        # recorded for My posts: only the destination has posts, exactly as they look there
        assert await db.shift_pairs("s1") == [(1, 11), (2, 12), (3, 13), (5, 14), (6, 15), (7, 16), (8, 17)]
        assert await db.sent_posts(1) == []
        mine = {p.message_id: p for p in await db.sent_posts(2)}
        assert sorted(mine) == [11, 12, 13, 14, 15, 16, 17] and mine[12].source == "bot"
        assert mine[12].buttons == [[{"t": "Download", "u": "https://t.me/goku?start=AAA"}]]
        assert mine[13].entities[0]["u"] == "https://t.me/dstch/14"
        assert mine[17].text == new[17].message and mine[13].media_kind == "photo"
        await db.close()

    run(go())


def test_only_the_posts_in_the_range_are_copied(tmp_path):
    async def go():
        src, dst = two_channels()
        db, dst_row, shift = await engine_setup(tmp_path, src, first=2, last=3)
        plan = await plan_repost(src, SRC, RepostOptions(), first_id=2, last_id=3)
        assert (plan.first, plan.last, plan.units, plan.partial) == (2, 3, 2, True)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed) == (2, [])
        assert [m.message for i, m in sorted(dst.msgs.items()) if i > 10] == ["Click below", "Caption with link"]
        await db.close()

    run(go())


def test_a_reply_to_a_post_outside_the_range_is_copied_without_the_reply(tmp_path):
    async def go():
        src, dst = two_channels()
        db, dst_row, shift = await engine_setup(tmp_path, src, first=5, last=5)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.replies, res.reply_dropped) == (1, 0, 1)
        assert reply_of(dst.msgs[11]) is None
        await db.close()

    run(go())


def test_a_source_that_forbids_saving_is_rebuilt_from_its_parts(tmp_path):
    async def go():
        src, dst = two_channels()
        src.restrict_forwards = True
        db, dst_row, shift = await engine_setup(tmp_path, src)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.failed, res.forwarded, res.rebuilt) == ([], 0, 6)
        new = {i: m for i, m in dst.msgs.items() if i > 10}
        assert new[15].grouped_id == new[16].grouped_id and new[13].media.photo.id == 31
        assert reply_of(new[14]) == 12
        await db.close()

    run(go())


def test_a_stopped_shift_continues_without_copying_anything_twice(tmp_path):
    async def go():
        src, dst = two_channels()
        db, dst_row, shift = await engine_setup(tmp_path, src)
        calls = []

        async def prog(res):
            calls.append(res.copied_units)

        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0, progress=prog, should_stop=lambda: len(calls) >= 2)
        assert res.stopped and res.copied_units == 2 and res.final is None
        again = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (again.copied_units, again.skipped_done, again.failed) == (4, 2, [])
        assert sorted(i for i in dst.msgs if i > 10) == [11, 12, 13, 14, 15, 16, 17]  # no duplicates
        # the reply of the 3rd copy found the copy made in the first run
        assert reply_of(dst.msgs[14]) == 12
        await db.close()

    run(go())


def test_undo_removes_only_the_copies(tmp_path):
    async def go():
        src, dst = two_channels()
        db, dst_row, shift = await engine_setup(tmp_path, src)
        await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        res = await delete_shift_copies(src, db, dst_row, shift, delay=0)
        assert (res.deleted, res.remaining, res.error) == (7, 0, None)
        assert sorted(dst.msgs) == list(range(1, 11)) and sorted(src.msgs) == [1, 2, 3, 4, 5, 6, 7, 8]
        assert await db.sent_posts(2) == [] and await db.shift_pairs("s1") == []
        assert (await db.shift_counts("s1"))["new_left"] == 0
        await db.close()

    run(go())


def test_a_post_that_was_shifted_before_is_known(tmp_path):
    async def go():
        src, dst = two_channels()
        db, dst_row, shift = await engine_setup(tmp_path, src)
        await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert await db.shifted_ids(1, 2, 1, 5) == {1, 2, 3, 5}
        assert await db.shifted_ids(1, 3, 1, 5) == set() and await db.shifted_ids(2, 1, 1, 5) == set()
        await db.mark_shift_deleted("s1", 2, [11])
        assert await db.shifted_ids(1, 2, 1, 5) == {2, 3, 5}
        await db.close()

    run(go())


# ---------------------------------------------------------------------------- as the owner experiences it
async def shift_app(tmp_path, **cfg):
    cfg.setdefault("edit_delay", 0.0)
    app = App(tmp_path, **cfg)
    await app.start()
    app.tg.username, app.tg.title = "srcch", "Anime Source"
    fill(app.tg, source_posts())
    dst = app.tg.add_channel(FakeChannelClient(old_posts(), username="dstch", cid=2, title="Anime Backup"))
    await app.db.save_channel(1, 5, "Anime Source", "srcch", 1)
    await app.db.save_channel(2, 6, "Anime Backup", "dstch", 1)
    return app, dst


def test_usage_and_bad_commands_are_explained(tmp_path):
    async def go():
        app, _ = await shift_app(tmp_path)
        await app.text("/shift")
        text = norm(app.out.last_text)
        assert "Usage: /shift <source> <destination> [from_id] [to_id]" in text and "15 20" in text
        for args, part in (
            ("@srcch", "Give the source and the destination"),
            ("@srcch @dstch x", "“x” is not a message id"),
            ("@srcch @srcch", "same channel"),
            ("@srcch @nosuch", "couldn't find a channel"),
        ):
            await app.text(f"/shift {args}")
            assert part in norm(app.out.last_text), (args, norm(app.out.last_text))
        assert not any(isinstance(r, functions.messages.SendMessageRequest) for r in app.tg.requests)
        await app.db.close()

    run(go())


def test_the_plan_is_shown_before_anything_is_copied(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        await app.text("/shift @srcch @dstch 2 5")
        text = norm(app.out.last_text)
        assert "Shift plan" in text and "From Anime Source → Anime Backup" in text
        assert "Post ids 2-5" in text and "3 posts to copy" in text  # 2, 3 and 5 (4 is a service message)
        assert "1 service message(s)" in text and "1 post(s) answer another post" in text
        assert "1 link(s) in 1 post(s) point at other posts of Anime Source" in text
        assert "Nothing is changed in Anime Source" in text
        assert dst.requests == [] or not any(isinstance(r, functions.messages.SendMessageRequest) for r in dst.requests)
        assert sorted(dst.msgs) == list(range(1, 11))
        await app.db.close()

    run(go())


def test_cancel_and_an_old_button(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        await app.text("/shift @srcch @dstch")
        start = app.out.callback_data("Start copying")
        await app.press(app.out.callback_data("Cancel"))
        assert "Cancelled" in norm(app.out.last_text)
        await app.press(start)
        assert "expired" in norm(app.out.last_text)
        assert sorted(dst.msgs) == list(range(1, 11))
        await app.db.close()

    run(go())


def test_the_whole_flow_copy_then_undo(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        await app.text("/shift @srcch @dstch")
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "Anime Source → Anime Backup: 7 message(s) copied" in done and "registered in My posts" in done
        assert "The source was not changed" in done and "1 link(s) in 1 post(s) now point at the new copies" in done
        assert sorted(i for i in dst.msgs if i > 10) == [11, 12, 13, 14, 15, 16, 17]
        assert len(await app.db.sent_posts(2)) == 7

        await app.press(app.out.callback_data("Undo"))
        assert "Remove the 7 copied message(s) from Anime Backup" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Yes, remove"))
        assert "7 copied message(s) removed from Anime Backup" in norm(app.out.last_text)
        assert sorted(dst.msgs) == list(range(1, 11)) and await app.db.sent_posts(2) == []
        sh = await app.db.open_shift()
        assert sh is None  # nothing is left open
        await app.db.close()

    run(go())


def test_a_destination_the_bot_does_not_know_yet_becomes_one_of_its_channels(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        await app.db.set_channel_active(2, False)
        await app.text("/shift @srcch @dstch 1 1")
        await app.press(app.out.callback_data("Start copying"))
        assert "1 message(s) copied" in norm(app.out.last_text)
        assert [c.id for c in await app.db.list_channels()] == [2, 1] or {c.id for c in await app.db.list_channels()} == {1, 2}
        await app.db.close()

    run(go())


def test_the_bot_must_be_allowed_to_post_in_the_destination(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        dst.rights = dict(admin=True, post=False, edit=True, delete=True, invite=False, add_admins=False)
        await app.text("/shift @srcch @dstch")
        assert "needs the “Post messages” admin right in Anime Backup" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_an_unreadable_source_is_explained(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        app.tg.fail["GetMessagesRequest"] = __import__("telethon").errors.ChannelPrivateError(request=None)
        await app.text("/shift @srcch @dstch")
        text = norm(app.out.last_text)
        assert "I can't read Anime Source" in text and "member of the source channel" in text
        await app.db.close()

    run(go())


def test_an_unfinished_shift_must_be_dealt_with_first(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        await app.db.create_shift("old1", src=SRC, dst_channel_id=2, first_id=1, last_id=8, user_id=1)
        await app.db.set_shift_status("old1", "stopped")
        await app.text("/shift @srcch @dstch")
        text = norm(app.out.last_text)
        assert "unfinished shift between these two channels" in text and "stopped" in text
        assert "Continue" in " ".join(t for row in app.out.last_buttons() for t in row)
        await app.text("/shift")  # without arguments it shows the same unfinished shift
        assert "Unfinished shift" in norm(app.out.last_text)
        # Continue copies what is left; the shift is over afterwards
        await app.press(app.out.callback_data("Continue"))
        assert "7 message(s) copied" in norm(app.out.last_text)
        assert await app.db.open_shift() is None
        await app.db.close()

    run(go())


def test_copying_the_same_posts_twice_is_pointed_out(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path)
        await app.text("/shift @srcch @dstch 1 2")
        await app.press(app.out.callback_data("Start copying"))
        await app.text("/shift @srcch @dstch 1 3")
        assert "2 of these posts were already shifted into Anime Backup before" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_shift_is_for_the_owner_only(tmp_path):
    async def go():
        app, dst = await shift_app(tmp_path, admins=frozenset({7}))
        app.uid = 7
        await app.text("/shift @srcch @dstch")
        assert "Shift plan" not in norm(app.out.last_text or "")
        assert sorted(dst.msgs) == list(range(1, 11))
        await app.db.close()

    run(go())
