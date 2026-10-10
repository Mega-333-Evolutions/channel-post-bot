"""/shift -c (links to the source's posts follow the copies, in every connected channel) and -all (the destination takes
over the source's name, description and photo)."""
import asyncio

from telethon import functions, types

from app.crosslinks import relink_after_shift, restore_after_shift_undo
from app.profile_copy import copy_profile, profile_lines
from app.shift_engine import delete_shift_copies, run_shift, shift_profile, source_of
from app.sync_engine import live_fields
from app.tgutil import button_url

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, make_msg, markup, rpc, url_btn
from .harness import norm
from .test_shift import SRC, shift_app, two_channels


def run(coro):
    return asyncio.run(coro)


T1 = "Watch https://t.me/srcch/3 here"


def other_posts():
    """A third connected channel with every kind of link to the source's posts."""
    return {
        1: make_msg(1, T1, [types.MessageEntityBold(T1.index("here"), 4)], out=True),
        2: make_msg(2, "Fifth", [types.MessageEntityTextUrl(0, 5, "https://t.me/srcch/5?single")], out=True),
        3: make_msg(
            3, "Pick",
            markup=markup([url_btn("Second", "https://t.me/srcch/2"), url_btn("Site", "https://example.com/x")]), out=True,
        ),
        4: make_msg(4, "Private https://t.me/c/1/8 form", out=True),
        5: make_msg(5, "Never shifted https://t.me/srcch/99", out=True),  # no copy of 99: stays
        6: make_msg(6, "Already there https://t.me/dstch/3", out=True),  # a link to the destination: stays
        7: make_msg(7, "By a friend https://t.me/srcch/1", out=False),
        8: make_msg(8, "No link", out=True),
    }


async def extras_setup(tmp_path, *marks, first=1, last=8):
    """source (id 1, registered), destination (id 2) and another channel (id 3), a shift s1 with the given marks."""
    src, dst = two_channels()
    other = src.add_channel(FakeChannelClient(other_posts(), username="otherch", cid=3, title="Other"))
    dst.msgs[5] = make_msg(5, "Old post: https://t.me/srcch/1", out=True)  # the destination had a link already
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Anime Source", "srcch", 1)
    await db.save_channel(2, 6, "Anime Backup", "dstch", 1)
    await db.save_channel(3, 7, "Other", "otherch", 1)
    for m in other.msgs.values():
        if m.out:
            await db.create_post(
                channel_id=3, message_id=m.id, status="sent", source="bot", created_by=1, link_preview=False, **live_fields(m)
            )
    await db.create_shift("s1", src=SRC, dst_channel_id=2, first_id=first, last_id=last, user_id=1)
    for mark in marks:
        await db.set_mark("s1", mark)
    return src, dst, other, db, await db.get_channel(2), await db.get_shift("s1")


def edited(chan):
    return [r.id for r in chan.requests if isinstance(r, functions.messages.EditMessageRequest)]


# ===================================================================================================== -c
def test_links_to_the_source_follow_the_copies_in_every_connected_channel(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "relink")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.aborted, res.cross_error) == (6, [], None, None)
        # copies: 1->11 2->12 3->13 5->14 6->15 7->16 8->17
        assert res.phase == "links" and res.cross is not None and res.cross.clean

        # a typed link, with the bold word after it moved along (the link got one character longer)
        assert other.msgs[1].message == "Watch https://t.me/dstch/13 here"
        bold = other.msgs[1].entities[0]
        assert other.msgs[1].message[bold.offset : bold.offset + bold.length] == "here"
        # a hyperlink behind text keeps its text and its ?single
        link = other.msgs[2].entities[0]
        assert link.url == "https://t.me/dstch/14?single" and (link.offset, link.length) == (0, 5)
        # a button changes, the other button stays
        row = other.msgs[3].reply_markup.rows[0].buttons
        assert button_url(row[0]) == "https://t.me/dstch/12" and row[0].text == "Second"
        assert button_url(row[1]) == "https://example.com/x"
        # the private form of the link, and a post made by somebody else (the bot may edit those here)
        assert other.msgs[4].message == "Private https://t.me/dstch/17 form"
        assert other.msgs[7].message == "By a friend https://t.me/dstch/11"
        # nothing else was touched: no copy / a link to the destination / no link
        assert other.msgs[5].message == "Never shifted https://t.me/srcch/99"
        assert other.msgs[6].message == "Already there https://t.me/dstch/3" and other.msgs[8].message == "No link"
        assert edited(other) == [1, 2, 3, 4, 7]
        # the destination's own older post was fixed too
        assert dst.msgs[5].message == "Old post: https://t.me/dstch/11"
        # the source is only read - although the bot knows it as one of its channels and its posts link to each other
        assert edited(src) == [] and src.delete_requests == []
        assert src.msgs[8].message == "See https://t.me/srcch/1 and https://t.me/c/1/2 and https://t.me/elsewhere/9"
        # My posts follows what the channel shows
        row1 = await db.find_by_message(3, 1)
        assert row1.text == other.msgs[1].message
        assert await db.has_mark("s1", "links")
        assert sorted(c.channel.id for c in res.cross.changed_channels) == [2, 3]
        await db.close()

    run(go())


def test_without_the_c_option_only_the_copies_themselves_are_fixed(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.cross is None and res.profile is None
        assert edited(other) == [] and other.msgs[1].message == T1
        assert dst.msgs[5].message == "Old post: https://t.me/srcch/1"  # an older post of the destination stays
        assert not await db.has_mark("s1", "links")
        await db.close()

    run(go())


def test_only_the_posts_that_were_shifted_have_their_links_changed(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "relink", first=1, last=2)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        # only the posts 1 and 2 were shifted (copies 11 and 12): two posts of the other channel and the old post of the
        # destination link to them
        assert res.copied_units == 2 and res.cross.relinked == 3
        assert other.msgs[7].message == "By a friend https://t.me/dstch/11"
        assert button_url(other.msgs[3].reply_markup.rows[0].buttons[0]) == "https://t.me/dstch/12"
        assert dst.msgs[5].message == "Old post: https://t.me/dstch/11"
        assert edited(other) == [3, 7]
        # the links to posts that were not shifted stay as they are
        assert other.msgs[1].message == T1
        assert other.msgs[2].entities[0].url == "https://t.me/srcch/5?single"
        assert other.msgs[4].message == "Private https://t.me/c/1/8 form"
        await db.close()

    run(go())


def test_the_links_can_be_updated_again_and_nothing_changes_twice(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "relink")
        await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        n = len(edited(other))
        again = await relink_after_shift(src, db, shift, source_of(shift), dst_row, delay=0, settle_pause=0, pause=0)
        assert again.relinked == 0 and again.clean and len(edited(other)) == n
        late = other._new("Late: https://t.me/srcch/6", None, None)
        late.out = False
        again = await relink_after_shift(src, db, shift, source_of(shift), dst_row, delay=0, settle_pause=0, pause=0)
        assert again.relinked == 1 and other.msgs[late.id].message == "Late: https://t.me/dstch/15"
        await db.close()

    run(go())


def test_undo_points_the_links_back_at_the_source(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "relink")
        before = {i: (m.message, [e.to_dict() for e in (m.entities or [])]) for i, m in other.msgs.items()}
        await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        back = await restore_after_shift_undo(src, db, shift, source_of(shift), dst_row, delay=0, settle_pause=0, pause=0)
        assert back.relinked == 6 and back.clean  # the five posts of the other channel and the old post of the destination
        for i, (text, ents) in before.items():
            if i == 4:  # the private form t.me/c/1/8 comes back as the public form of the same post (the source has a name)
                assert other.msgs[i].message == "Private https://t.me/srcch/8 form"
                continue
            assert other.msgs[i].message == text and [e.to_dict() for e in (other.msgs[i].entities or [])] == ents, i
        assert button_url(other.msgs[3].reply_markup.rows[0].buttons[0]) == "https://t.me/srcch/2"
        assert dst.msgs[5].message == "Old post: https://t.me/srcch/1"
        res = await delete_shift_copies(src, db, dst_row, shift, delay=0)
        assert res.remaining == 0 and res.deleted == 7
        await db.close()

    run(go())


def test_an_undo_without_c_reads_nothing(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path)
        await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        mark = len(other.requests)
        back = await restore_after_shift_undo(src, db, shift, source_of(shift), dst_row, delay=0, settle_pause=0, pause=0)
        assert back.channels == [] and len(other.requests) == mark
        await db.close()

    run(go())


# ================================================================================================== -all
def prepare_profiles(src, dst):
    src.about, dst.about = "Source about", "old about"
    src.set_photo(b"SRC-PHOTO")
    dst.set_photo(b"OLD-PHOTO")


def notices(chan):
    return [m for m in chan.msgs.values() if isinstance(m, types.MessageService)]


def test_the_destination_takes_over_name_description_and_photo(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "profile")
        prepare_profiles(src, dst)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.profile is not None and res.profile.ok and res.profile_error is None
        assert res.profile.outcome == {"name": ("done", "Anime Source"), "description": ("done", ""), "photo": ("done", "")}
        assert (dst.title, dst.about, dst.photo_bytes()) == ("Anime Source", "Source about", b"SRC-PHOTO")
        # the source was only read
        assert (src.title, src.about, src.photo_bytes()) == ("Anime Source", "Source about", b"SRC-PHOTO")
        assert not [r for r in src.requests if type(r).__name__.startswith("Edit")]
        # the "name changed" / "photo changed" notices Telegram adds are deleted again
        assert notices(dst) == []
        # the bot's own list knows the new name
        assert (await db.get_channel(2)).title == "Anime Source"
        assert await db.has_mark("s1", "profile_done")
        # the posts were copied as usual, and -c was not asked for
        assert res.copied_units == 6 and res.cross is None
        await db.close()

    run(go())


def test_it_is_done_once_not_again_when_a_stopped_shift_continues(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "profile")
        prepare_profiles(src, dst)
        calls = []

        async def prog(res):
            calls.append(res.copied_units)

        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0, progress=prog, should_stop=lambda: len(calls) >= 2)
        assert res.stopped and res.profile is None and dst.title == "Anime Backup"  # not before everything is copied
        again = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert again.profile is not None and again.profile.ok and dst.title == "Anime Source"
        titles = [r for r in dst.requests if isinstance(r, functions.channels.EditTitleRequest)]
        third = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)  # nothing left to do
        assert third.profile is None
        assert [r for r in dst.requests if isinstance(r, functions.channels.EditTitleRequest)] == titles
        await db.close()

    run(go())


def test_a_part_the_source_does_not_have_is_left_as_the_destination_has_it(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "profile")
        dst.title = "Anime Source"  # the same name already
        dst.about = "my own description"
        dst.set_photo(b"MY-OWN-PHOTO")  # the source has neither a description nor a photo
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.profile.outcome == {"name": ("same", ""), "description": ("none", ""), "photo": ("none", "")}
        assert (dst.title, dst.about, dst.photo_bytes()) == ("Anime Source", "my own description", b"MY-OWN-PHOTO")
        lines = "\n".join(profile_lines(res.profile))
        assert "the source has none - left as it is" in lines and "already the same" in lines
        await db.close()

    run(go())


def test_a_missing_admin_right_is_named_and_the_parts_can_be_tried_again(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "profile")
        prepare_profiles(src, dst)
        dst.rights = dict(admin=True, post=True, edit=True, delete=True, invite=False, add_admins=False, change_info=False)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.copied_units == 6 and res.failed == []  # the posts are there
        assert not res.profile.ok and [p for p, _ in res.profile.failed] == ["name", "description", "photo"]
        text = "\n".join(profile_lines(res.profile))
        assert "Change channel info" in text and "Not copied" in text
        assert dst.title == "Anime Backup" and dst.about == "old about" and dst.photo_bytes() == b"OLD-PHOTO"
        assert not await db.has_mark("s1", "profile_done")
        # the right is given: the same run again (the "copy again" button) does it
        dst.rights["change_info"] = True
        holder = type(res)()
        await shift_profile(src, db, shift, dst_row, 1, holder)
        assert holder.profile.ok and (dst.title, dst.about, dst.photo_bytes()) == ("Anime Source", "Source about", b"SRC-PHOTO")
        assert await db.has_mark("s1", "profile_done")
        await db.close()

    run(go())


def test_one_part_failing_does_not_stop_the_others(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "profile")
        prepare_profiles(src, dst)
        dst.fail["EditPhotoRequest"] = rpc("PhotoCropSizeSmallError")
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.profile.outcome["name"][0] == "done" and res.profile.outcome["description"][0] == "done"
        assert res.profile.outcome["photo"] == ("failed", "PhotoCropSizeSmallError")
        assert (dst.title, dst.about, dst.photo_bytes()) == ("Anime Source", "Source about", b"OLD-PHOTO")
        assert not await db.has_mark("s1", "profile_done")  # so the button to try again is offered
        assert (await db.get_channel(2)).title == "Anime Source"  # what was changed is known to the bot
        await db.close()

    run(go())


def test_an_unreadable_source_fails_every_part_and_changes_nothing(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "profile")
        prepare_profiles(src, dst)
        src.fail["GetFullChannelRequest"] = rpc("ChannelPrivateError")
        res = await copy_profile(src, source_of(shift), dst_row)
        assert [p for p, _ in res.failed] == ["name", "description", "photo"] and "can't be read" in res.failed[0][1]
        assert (dst.title, dst.about, dst.photo_bytes()) == ("Anime Backup", "old about", b"OLD-PHOTO")
        await db.close()

    run(go())


def test_both_extras_together(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "relink", "profile")
        prepare_profiles(src, dst)
        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert res.profile.ok and res.cross.relinked == 6 and res.cross_error is None and res.profile_error is None
        assert other.msgs[1].message == "Watch https://t.me/dstch/13 here" and dst.title == "Anime Source"
        await db.close()

    run(go())


# ========================================================================================= the owner's view
async def extras_app(tmp_path):
    app, dst = await shift_app(tmp_path)
    other = app.tg.add_channel(FakeChannelClient(other_posts(), username="otherch", cid=3, title="Other"))
    await app.db.save_channel(3, 7, "Other", "otherch", 1)
    dst.msgs[5] = make_msg(5, "Old post: https://t.me/srcch/1", out=True)
    app.tg.about, dst.about = "Source about", "old about"
    app.tg.set_photo(b"SRC-PHOTO")
    dst.set_photo(b"OLD-PHOTO")
    return app, dst, other


def labels(app):
    return [t for row in app.out.last_buttons() for t in row]


def test_the_plan_names_the_extras_that_were_asked_for(tmp_path):
    async def go():
        app, dst, other = await extras_app(tmp_path)
        await app.text("/shift @srcch @dstch")
        plain = norm(app.out.last_text)
        assert "-c" not in plain and "-all" not in plain

        await app.text("/shift @srcch @dstch -c")
        text = norm(app.out.last_text)
        assert "-c: afterwards every link to a post of Anime Source" in text and "in your 2 other connected channel(s)" in text
        assert "Anime Source itself is not changed" in text and "-all" not in text

        await app.text("/shift @srcch @dstch - all")  # typed with a space
        text = norm(app.out.last_text)
        assert "-all: afterwards Anime Backup gets the name, description and profile photo of Anime Source" in text
        assert "undo does not bring them back" in text and "-c:" not in text and "Change channel info" not in text

        await app.text("/shift @srcch @dstch 2 5 -all -c")
        text = norm(app.out.last_text)
        assert "Post ids 2-5" in text and "-c: afterwards" in text and "-all: afterwards" in text
        assert dst.title == "Anime Backup" and edited(other) == []  # a plan changes nothing
        await app.db.close()

    run(go())


def test_the_plan_warns_when_the_info_right_is_missing(tmp_path):
    async def go():
        app, dst, other = await extras_app(tmp_path)
        dst.rights = dict(admin=True, post=True, edit=True, delete=True, invite=False, add_admins=False, change_info=False)
        await app.text("/shift @srcch @dstch -all")
        assert "does not have the “Change channel info” admin right in Anime Backup yet" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_an_unknown_option_is_explained(tmp_path):
    async def go():
        app, dst, other = await extras_app(tmp_path)
        await app.text("/shift @srcch @dstch -x")
        assert "Unknown option -x" in norm(app.out.last_text) and "-c and -all" in norm(app.out.last_text)
        assert dst.title == "Anime Backup"
        await app.db.close()

    run(go())


def test_the_whole_flow_with_both_extras_then_undo(tmp_path):
    async def go():
        app, dst, other = await extras_app(tmp_path)
        await app.text("/shift @srcch @dstch -c -all")
        await app.press(app.out.callback_data("Start copying"))
        done = norm(app.out.last_text)
        assert "7 message(s) copied" in done
        assert "link(s) in 6 post(s) of 2 channel(s) now point at the new copies" in done
        assert "Other: 5 link(s) in 5 post(s)" in done
        assert "name copied, description copied, profile photo copied" in done
        assert (dst.title, dst.about, dst.photo_bytes()) == ("Anime Source", "Source about", b"SRC-PHOTO")
        assert other.msgs[1].message == "Watch https://t.me/dstch/13 here"
        shown = labels(app)
        assert any("Update links again" in t for t in shown) and any("Undo" in t for t in shown)
        assert not any("again" in t and "photo" in t for t in shown)  # the profile was copied: no button for it

        # "update links again": nothing new, so nothing to change
        await app.press(app.out.callback_data("Update links again"))
        assert "no link there needed changing" in norm(app.out.last_text)

        # undo: the links go back to the source's posts first, then the copies are removed
        await app.press(app.out.callback_data("Undo"))
        await app.press(app.out.callback_data("Yes, remove"))
        text = norm(app.out.last_text)
        assert "7 copied message(s) removed from" in text and "6 post(s) of 2 channel(s) point back at the original posts" in text
        assert other.msgs[1].message == T1 and other.msgs[7].message == "By a friend https://t.me/srcch/1"
        assert dst.msgs[5].message == "Old post: https://t.me/srcch/1"
        assert dst.title == "Anime Source"  # the name is not taken back (the plan said so)
        await app.db.close()

    run(go())


def test_a_failed_profile_gets_its_own_button(tmp_path):
    async def go():
        app, dst, other = await extras_app(tmp_path)
        dst.rights = dict(admin=True, post=True, edit=True, delete=True, invite=False, add_admins=False, change_info=False)
        await app.text("/shift @srcch @dstch -all")
        await app.press(app.out.callback_data("Start copying"))
        text = norm(app.out.last_text)
        assert "7 message(s) copied" in text and "Not copied" in text and "Change channel info" in text
        assert any("Copy name, description, photo again" in t for t in labels(app))
        dst.rights["change_info"] = True
        await app.press(app.out.callback_data("Copy name, description, photo again"))
        text = norm(app.out.last_text)
        assert "name copied, description copied, profile photo copied" in text
        assert (dst.title, dst.about, dst.photo_bytes()) == ("Anime Source", "Source about", b"SRC-PHOTO")
        assert not any("photo again" in t for t in labels(app))  # done now
        await app.db.close()

    run(go())


def test_the_extra_buttons_only_exist_for_the_extras_that_were_asked_for(tmp_path):
    async def go():
        app, dst, other = await extras_app(tmp_path)
        await app.text("/shift @srcch @dstch")
        await app.press(app.out.callback_data("Start copying"))
        shown = " | ".join(labels(app))
        assert "Update links again" not in shown and "photo again" not in shown and "Undo" in shown
        assert edited(other) == []
        await app.db.close()

    run(go())


def test_the_extras_that_were_asked_for_are_remembered_with_the_shift(tmp_path):
    async def go():
        app, dst, other = await extras_app(tmp_path)
        await app.text("/shift @srcch @dstch -c")
        await app.press(app.out.callback_data("Start copying"))
        sid = app.out.callback_data("Undo").split(":")[1]
        assert await app.db.marks_of(sid) == {"relink", "links"}
        await app.db.close()

    run(go())


def test_a_stopped_shift_that_is_continued_still_updates_the_links(tmp_path):
    async def go():
        src, dst, other, db, dst_row, shift = await extras_setup(tmp_path, "relink")
        calls = []

        async def prog(res):
            calls.append(res.copied_units)

        res = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0, progress=prog, should_stop=lambda: len(calls) >= 2)
        assert res.stopped and res.cross is None and edited(other) == []  # the links wait until everything is copied
        again = await run_shift(src, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert not again.stopped and again.cross is not None and again.cross.relinked == 6
        assert other.msgs[1].message == "Watch https://t.me/dstch/13 here"
        await db.close()

    run(go())
