"""What TELEGRAM does to a channel (a copyright strike ...) is not an admin's edit: My posts stay exactly as saved."""
from telethon import types

from app.restrictions import PLACEHOLDER_NOTE, is_placeholder_text, message_restriction, restriction_of
from app.sync_engine import MASS_NOTE, diff_post, split_mass_text, sync_channel, sync_ids

from .fakes import markup, url_btn
from .harness import norm
from .test_sync import foreign, looked, old_msg, opts, run, world
from .test_syncer import dms, make_app, three as three_posts

NOTICE = "This message couldn't be displayed on your device due to copyright infringement."


def strike(m, text=NOTICE):
    """Telegram holds a message back: its own restriction mark on it."""
    m.restriction_reason = [types.RestrictionReason(platform="all", reason="copyright", text=text)]
    return m


def seven():
    return {i: old_msg(i, f"post number {i}", markup=markup([url_btn("Watch", f"https://t.me/a/{i}")])) for i in range(1, 8)}


# ================================================================================================ recognising it
def test_the_notice_telegram_shows_instead_of_a_post_is_recognised():
    for text in (
        NOTICE,
        "This message couldn’t be displayed on your device due to copyright infringement",
        "  This message  couldn't be displayed\non your device due to copyright infringement.  ",
        "This channel can't be displayed because it violated Telegram's Terms of Service.",
        "This post isn't available in your country due to copyright.",
    ):
        assert is_placeholder_text(text), text
    for text in (
        "",
        None,
        "Episode 12 is out now",
        "This video is not available in HD",  # says "not available" but nothing about a restriction
        "Watch: this message couldn't be displayed on your device",  # not the notice itself
        "This message couldn't be displayed on your device due to copyright infringement. " + "x" * 300,  # a long post
    ):
        assert not is_placeholder_text(text), text


def test_telegrams_own_marks_are_read_from_messages_and_channels():
    plain = old_msg(1, "hello")
    assert restriction_of(plain) is None and message_restriction(plain) is None
    held = strike(old_msg(2, "hello"), "Removed after a copyright complaint")
    assert restriction_of(held) == "Removed after a copyright complaint"
    assert message_restriction(held) == "Removed after a copyright complaint"
    two = old_msg(3, "x")
    two.restriction_reason = [
        types.RestrictionReason("ios", "sensitive", "Sensitive content"),
        types.RestrictionReason("android", "sensitive", "Sensitive content"),  # the same words twice: said once
        types.RestrictionReason("all", "copyright", ""),  # no words: its code is used
    ]
    assert restriction_of(two) == "Sensitive content; copyright"
    nameless = old_msg(4, "x")
    nameless.restriction_reason = [types.RestrictionReason("all", "terms", "")]
    assert restriction_of(nameless) == "terms"
    chan = types.Channel(id=1, title="t", photo=types.ChatPhotoEmpty(), date=None, restricted=True)
    assert restriction_of(chan) == "restricted by Telegram"
    assert restriction_of(types.Channel(id=1, title="t", photo=types.ChatPhotoEmpty(), date=None)) is None


def test_a_text_the_saved_post_already_has_is_the_owners_own_wording():
    m = old_msg(1, NOTICE)
    assert message_restriction(m) == PLACEHOLDER_NOTE  # an unknown message: it is the notice
    assert message_restriction(m, "post number 1") == PLACEHOLDER_NOTE  # the saved post said something else
    assert message_restriction(m, NOTICE) is None  # the saved post says exactly that: nothing changed


def test_many_posts_that_suddenly_show_one_text_are_held_back_but_few_are_not(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, {i: old_msg(i, f"post number {i}") for i in range(1, 8)})
        rows = {p.message_id: p for p in await db.sent_posts(1)}

        def edits_to(text, ids):
            return [(rows[i], diff_post(rows[i], old_msg(i, text))) for i in ids]

        keep, held = split_mass_text(edits_to("Gone.", range(1, 6)))
        assert keep == [] and len(held) == 5
        keep, held = split_mass_text(edits_to("Gone.", range(1, 5)))  # four are below the line
        assert len(keep) == 4 and held == []
        keep, held = split_mass_text(edits_to("", range(1, 8)))  # all texts removed: a person can do that
        assert len(keep) == 7 and held == []
        keep, held = split_mass_text(edits_to("Gone.", range(1, 6)) + edits_to("Other", [6, 7]))
        assert [p.message_id for p, _ in keep] == [6, 7] and len(held) == 5
        await db.close()

    run(go())


def test_edits_that_all_have_the_same_text_but_the_same_saved_text_are_a_real_edit(tmp_path):
    async def go():
        msgs = {i: old_msg(i, "Join us") for i in range(1, 8)}  # seven posts that said exactly the same
        chan, db, ch = await world(tmp_path, msgs)
        for m in msgs.values():
            m.message = "Join us now"
        rep = await sync_channel(chan, db, ch, opts())
        assert len(rep.edited) == 7 and rep.restricted == []
        await db.close()

    run(go())


# =============================================================================================== the whole channel
def test_a_channel_telegram_restricted_is_left_alone(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        before = await db.get_sync(1)
        chan.restriction = NOTICE
        del chan.msgs[2]
        chan.msgs[1].message = "edited by an admin"
        new = foreign(chan, "a new post")
        chan._pts += 2
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.restricted_channel == NOTICE and rep.error is None
        assert (rep.deleted, rep.edited, rep.adopted, rep.restricted) == ([], [], [], [])
        assert sorted(p.message_id for p in await db.sent_posts(1)) == list(range(1, 8))  # nothing removed
        assert (await db.find_by_message(1, 1)).text == "post number 1"  # nothing edited
        assert await db.find_by_message(1, new.id) is None  # nothing added
        after = await db.get_sync(1)
        assert (after.last_top, after.last_pts) == (before.last_top, before.last_pts)  # the mark did not move

        chan.restriction = None  # the strike is lifted: the next look is the normal one again
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.restricted_channel is None
        assert (rep.deleted, rep.edited, rep.adopted) == ([2], [1], [new.id])
        await db.close()

    run(go())


def test_the_first_look_at_a_restricted_channel_changes_nothing_either(tmp_path):
    async def go():
        chan, db, ch = await world(tmp_path, seven())
        chan.restriction = NOTICE
        for m in chan.msgs.values():
            m.message = NOTICE
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.restricted_channel == NOTICE and rep.first_look is False
        assert await db.get_sync(1) is None
        assert [p.text for p in await db.sent_posts(1)] == [f"post number {i}" for i in range(1, 8)]
        await db.close()

    run(go())


# ============================================================================================ a copyright strike
def test_every_post_turning_into_the_notice_changes_nothing_in_my_posts(tmp_path):
    """The case that was asked about: Telegram swaps the text of EVERY post for its notice."""

    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        for m in chan.msgs.values():
            m.message, m.entities, m.reply_markup = NOTICE, None, None  # what the strike leaves in the channel
        chan._pts += 7
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.edited == [] and rep.deleted == [] and rep.buttons_missing == []
        assert sorted(rep.restricted) == list(range(1, 8)) and rep.restricted_why == PLACEHOLDER_NOTE
        for i, p in enumerate(await db.sent_posts(1), 1):
            assert p.text == f"post number {i}" and p.buttons == [[{"t": "Watch", "u": f"https://t.me/a/{i}"}]]
        await db.close()

    run(go())


def test_posts_carrying_telegrams_restriction_mark_are_left_as_saved(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        for i in (2, 3):
            strike(chan.msgs[i])
            chan.msgs[i].message = "(something else)"
        chan.msgs[4].message = "a real edit by an admin"
        chan._pts += 3
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.restricted) == [2, 3] and rep.restricted_why == NOTICE
        assert rep.edited == [4]  # everything else is looked at as usual
        assert (await db.find_by_message(1, 2)).text == "post number 2"
        assert (await db.find_by_message(1, 4)).text == "a real edit by an admin"
        await db.close()

    run(go())


def test_a_post_that_is_really_gone_is_still_removed_while_others_are_held_back(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        strike(chan.msgs[2])
        chan.msgs[2].message = NOTICE
        del chan.msgs[6]
        chan._pts += 2
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.deleted == [6] and rep.restricted == [2]
        assert (await db.find_by_message(1, 2)).text == "post number 2"
        await db.close()

    run(go())


def test_a_notice_does_not_stop_a_normal_edit_of_another_post(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        chan.msgs[1].message = NOTICE
        chan.msgs[5].message = "the admin fixed a typo"
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.restricted == [1] and rep.edited == [5]
        await db.close()

    run(go())


def test_five_posts_that_all_show_the_same_unknown_text_are_held_back(tmp_path):
    """The notice in another language: it is not in any list, but nobody edits five posts into one text."""

    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        for i in range(1, 6):
            chan.msgs[i].message = "Este mensaje no se puede mostrar en tu dispositivo."
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.restricted) == [1, 2, 3, 4, 5] and rep.edited == [] and rep.restricted_why == MASS_NOTE
        assert (await db.find_by_message(1, 1)).text == "post number 1"
        await db.close()

    run(go())


def test_four_posts_with_the_same_new_text_are_ordinary_edits(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        for i in range(1, 5):
            chan.msgs[i].message = "Moved to the new channel."
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.edited) == [1, 2, 3, 4] and rep.restricted == []
        await db.close()

    run(go())


# ================================================================================================= new posts
def test_new_posts_that_telegram_holds_back_are_not_added(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, three_posts_dict())
        a = foreign(chan, "a normal new post")
        b = strike(foreign(chan, "hidden by Telegram"))
        c = foreign(chan, NOTICE)
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.adopted == [a.id] and sorted(rep.restricted) == [b.id, c.id]
        assert await db.find_by_message(1, b.id) is None and await db.find_by_message(1, c.id) is None
        again = await sync_channel(chan, db, ch, opts())  # looked at once, not over and over
        assert again.restricted == [] and again.adopted == []
        await db.close()

    run(go())


def three_posts_dict():
    return {m.id: m for m in three_posts()}


# =============================================================================================== the live look
def test_the_live_look_leaves_a_restricted_post_alone(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        strike(chan.msgs[3])
        chan.msgs[3].message = NOTICE
        chan.msgs[4].message = "a real edit"
        rep = await sync_ids(chan, db, ch, [3, 4], opts())
        assert rep.restricted == [3] and rep.edited == [4]
        assert (await db.find_by_message(1, 3)).text == "post number 3"
        await db.close()

    run(go())


def test_the_live_look_holds_back_a_burst_of_identical_texts(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        for i in range(1, 6):
            chan.msgs[i].message = "Blocked."
        rep = await sync_ids(chan, db, ch, [1, 2, 3, 4, 5], opts())
        assert sorted(rep.restricted) == [1, 2, 3, 4, 5] and rep.edited == []
        await db.close()

    run(go())


def test_the_live_look_does_not_add_a_restricted_new_post(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        m = foreign(chan, NOTICE)
        rep = await sync_ids(chan, db, ch, [m.id], opts(), adopt_ids=[m.id])
        assert rep.adopted == [] and rep.restricted == [m.id]
        await db.close()

    run(go())


# ============================================================================== what "everything looks deleted" means
def test_posts_really_deleted_by_an_admin_all_go_even_when_that_is_everything(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        chan.msgs.clear()
        chan._pts += 7  # seven deletions are seven events
        rep = await sync_channel(chan, db, ch, opts())
        assert sorted(rep.deleted) == list(range(1, 8)) and rep.anomaly is None
        assert await db.sent_posts(1) == []
        await db.close()

    run(go())


def test_posts_that_merely_vanish_from_view_are_not_removed_without_the_events(tmp_path):
    async def go():
        chan, db, ch = await looked(tmp_path, seven())
        chan.msgs.clear()  # nothing happened in the channel: the bot just sees nothing
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.deleted == [] and rep.anomaly and "All 7" in rep.anomaly
        chan._pts += 3  # three events are not enough to explain seven missing posts
        rep = await sync_channel(chan, db, ch, opts())
        assert rep.deleted == [] and rep.anomaly
        assert len(await db.sent_posts(1)) == 7
        await db.close()

    run(go())


# ================================================================================================= the owners
def test_sync_tells_what_it_left_alone_in_a_restricted_channel(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        await app.text("/sync")  # the first look
        app.tg.restriction = NOTICE
        app.tg.msgs[2].message = NOTICE
        await app.text("/sync")
        text = norm(app.out.last_text)
        assert "Telegram has restricted this channel" in text and "copyright infringement" in text
        assert "stay exactly as they are" in text and "Everything matches" not in text
        assert (await app.db.find_by_message(1, 2)).text == "second"
        await app.db.close()

    run(go())


def test_sync_tells_which_posts_it_held_back(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        await app.text("/sync")
        strike(app.tg.msgs[2])
        app.tg.msgs[2].message = "(hidden)"
        await app.text("/sync")
        text = norm(app.out.last_text)
        assert "1 post(s) are held back by Telegram" in text and "left exactly as saved" in text and "message 2" in text
        assert "Everything matches" not in text
        await app.db.close()

    run(go())


def test_the_owners_are_told_about_a_restriction_once_not_every_check(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        sy = app.ctx.syncer
        app.tg.restriction = NOTICE
        await sy.run_all()
        await sy.run_all()
        await sy.run_all()
        told = [t for t in dms(app) if "restricted" in t]
        assert len(told) == 1 and "Anime Channel" in told[0] and "stay exactly as they are" in told[0]
        sy._told.clear()  # a week later
        await sy.run_all()
        assert len([t for t in dms(app) if "restricted" in t]) == 2
        await app.db.close()

    run(go())


def test_the_owners_are_told_about_held_back_posts_too(tmp_path):
    async def go():
        app = await make_app(tmp_path, posts=three_posts())
        sy = app.ctx.syncer
        await sy.run_all()
        strike(app.tg.msgs[3])
        await sy.run_all()
        await sy.run_all()
        told = [t for t in dms(app) if "holds back" in t]
        assert len(told) == 1 and "1 post(s)" in told[0]
        await app.db.close()

    run(go())
