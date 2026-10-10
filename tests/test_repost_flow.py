"""/repost as the owner experiences it: pick the channel with buttons, check the plan, copy, then delete the old
posts (by the bot, or by the userbot when the bot is not allowed to)."""
import asyncio
from types import SimpleNamespace

from app.tgutil import button_url
from app.userbot import Userbot

from .fakes import FakeUserClient, make_msg
from .harness import App, norm
from .test_repost import NEW, OLD, build

OLD_IDS = {1, 2, 3, 4, 5, 8, 9, 10}  # the posts of build() that /repost copies (6 is a service message)


def fill(tg, msgs):
    tg.msgs = dict(msgs)
    tg._top = max(msgs, default=0)
    for m in msgs.values():
        tg._remember_media(m)


async def make_app(tmp_path, msgs=None, **cfg):
    cfg.setdefault("edit_delay", 0.0)
    app = App(tmp_path, **cfg)
    await app.start()
    await app.db.save_channel(1, 5, "Test", "testch", 1)
    fill(app.tg, build() if msgs is None else msgs)
    return app


def buttons_text(app):
    return " | ".join(t for row in app.out.last_buttons() for t in row)


async def pick(app, args=f"@{OLD} @{NEW}"):
    await app.text(f"/repost {args}".strip())
    await app.press(app.out.callback_data("Test"))


async def copy_all(app):
    await pick(app)
    await app.press(app.out.callback_data("Start copying"))


def migration_id(app):
    return app.out.callback_data("Undo").split(":")[1]


# ---------------------------------------------------------------------------------------- choosing
def test_usage_does_not_mention_the_channel_option_any_more(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/repost")
        text = norm(app.out.last_text)
        assert "Usage" in text and "--channel" not in text and "buttons" in text
        await app.db.close()

    asyncio.run(run())


def test_the_old_channel_option_is_explained_not_obeyed(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text(f"/repost @{OLD} @{NEW} --channel @testch")
        assert "no longer takes --channel" in norm(app.out.last_text)
        assert app.tg.requests == []  # nothing was read or posted
        await app.db.close()

    asyncio.run(run())


def test_connected_channels_are_offered_as_a_numbered_list(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.db.save_channel(2, 6, "Second channel", None, 1)
        await app.text(f"/repost @{OLD} @{NEW}")
        text = norm(app.out.last_text)
        assert "Which channel" in text and f"@{OLD}" in text and f"@{NEW}" in text
        assert "1. Second channel" in text and "2. Test" in text  # the names are in the message, A to Z
        assert app.out.last_buttons() == [["1", "2"], ["✖️ Cancel"]]  # the buttons only carry the numbers
        first, second = app.out.callback_exact("1"), app.out.callback_exact("2")
        assert first.startswith("rpch:") and first.endswith(":2")  # number 1 is "Second channel", channel 2
        assert second.startswith("rpch:") and second.endswith(":1")
        assert app.tg.requests == []  # choosing is free: nothing is read before a button is pressed
        await app.db.close()

    asyncio.run(run())


def test_without_usernames_the_posts_are_copied_without_changing_links(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text("/repost")
        assert "Usage" in norm(app.out.last_text)
        await app.text("/repost --last 4")
        assert "copy without changing any link" in norm(app.out.last_text) and "newest 4" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Test"))
        assert "Repost plan" in norm(app.out.last_text) and "Trial run" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_no_registered_channel_is_explained(tmp_path):
    async def run():
        app = App(tmp_path)
        await app.start()
        await app.text(f"/repost @{OLD} @{NEW}")
        assert "No channels registered" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_bad_arguments(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        for bad, part in [
            (f"@{OLD}", "both usernames"),
            (f"@{OLD} @{OLD}", "are the same"),
            ("@a-b @c", "Usernames look like"),
            ("--last x", "--last needs a number"),
            ("--wat", "Unknown option"),
        ]:
            await app.text(f"/repost {bad}")
            assert part in norm(app.out.last_text), (bad, norm(app.out.last_text))
        await app.db.close()

    asyncio.run(run())


def test_the_list_can_be_cancelled_and_then_no_longer_works(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text(f"/repost @{OLD} @{NEW}")
        pick_data = app.out.callback_data("Test")
        await app.press(app.out.callback_data("Cancel"))
        assert "Cancelled" in norm(app.out.last_text)
        await app.press(pick_data)
        assert "expired" in norm(app.out.last_text)
        assert app.tg.requests == []
        await app.db.close()

    asyncio.run(run())


def test_a_forged_or_old_button_is_refused(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.press("rpch:deadbeef:1")
        assert "expired" in norm(app.out.last_text)
        await app.press("rpa:deadbeef")
        assert "expired" in norm(app.out.last_text)
        await app.press("rpdy:nosuch")
        assert "don't know that repost" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_only_the_owner_can_use_it(tmp_path):
    async def run():
        app = await make_app(tmp_path, admins=frozenset({7}))
        app.uid = 7  # an allowed helper account, but not an owner
        await app.text(f"/repost @{OLD} @{NEW}")
        assert "private" in norm(app.out.last_text)
        # a list made by the owner can't be used by somebody else, even by pressing the same button
        app.uid = 1
        await app.text(f"/repost @{OLD} @{NEW}")
        data = app.out.callback_data("Test")
        app.uid = 7
        await app.press(data)
        assert "Only the bot owner" in norm(app.out.last_text)
        await app.press("rpdy:whatever")
        assert "Only the bot owner" in norm(app.out.last_text)
        assert app.tg.requests == []
        await app.db.close()

    asyncio.run(run())


def test_the_bot_needs_the_post_right(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        app.tg.rights = dict(admin=True, post=False, edit=True, delete=True, invite=False, add_admins=False)
        await pick(app)
        assert "Post messages" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_another_long_job_blocks_a_new_plan(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text(f"/repost @{OLD} @{NEW}")
        data = app.out.callback_data("Test")
        await app.ctx.lock.acquire()
        await app.press(data)
        assert "still running" in norm(app.out.last_text)
        app.ctx.lock.release()
        await app.press(data)  # the list is still valid afterwards
        assert "Repost plan" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


# ------------------------------------------------------------------------------------------ plan
def test_the_plan_describes_what_will_happen(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await pick(app)
        text = norm(app.out.last_text)
        assert "Repost plan - Test" in text
        assert "7 posts to copy (8 messages, 1 album(s))" in text
        assert f"@{OLD} → @{NEW} in 4 post(s): 2 button link(s), 1 hyperlink(s), 1 typed link(s)" in text
        assert "1 service message(s)" in text
        assert "1 post(s) carry other bots' buttons" in text
        assert "read back" in text and "Nothing is deleted in this step" in text
        assert app.out.last_buttons() == [["▶️ Start copying", "✖️ Cancel"]]
        # planning only reads: nothing was copied, posted or deleted
        assert app.tg.requests and not any(
            type(r).__name__ in ("ForwardMessagesRequest", "SendMessageRequest", "SendMediaRequest", "DeleteMessagesRequest")
            for r in app.tg.requests
        )
        assert sorted(app.tg.msgs) == sorted(build())  # reading changed nothing
        await app.db.close()

    asyncio.run(run())


def test_plan_of_an_empty_channel(tmp_path):
    async def run():
        app = await make_app(tmp_path, msgs={})
        await pick(app)
        assert "⚠️" in norm(app.out.last_text) and "Start copying" not in buttons_text(app)
        await app.db.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------------- copy and result
def test_copy_everything_and_look_at_the_result(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        text = norm(app.out.last_text)
        assert "Test: all posts copied in order" in text and f"@{OLD} → @{NEW}" in text
        assert "8 messages are now at the end of the channel" in text
        assert "3 post(s) were copied by Telegram, 4 were posted again with their buttons" in text
        assert "Delete the old posts" in buttons_text(app) and "Undo" in buttons_text(app)
        # the channel: the originals are untouched and the 8 copies follow in order
        assert OLD_IDS <= set(app.tg.msgs)
        new = [app.tg.msgs[i] for i in sorted(app.tg.msgs) if i > 10]
        assert [m.message for m in new][0] == "Welcome" and [m.message for m in new][-1] == "Bye"
        with_buttons = [m for m in new if m.reply_markup is not None]
        assert len(with_buttons) == 2
        assert button_url(with_buttons[0].reply_markup.rows[0].buttons[0]) == f"https://t.me/{NEW}?start=AAA"
        mig = await app.db.get_migration(migration_id(app))
        assert mig.status == "copied" and not mig.partial
        # every copy is in My posts
        assert (await app.db.migration_counts(mig.id))["copied"] == 8
        await app.db.close()

    asyncio.run(run())


def test_a_second_press_on_start_does_not_copy_twice(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await pick(app)
        start = app.out.callback_data("Start copying")
        await app.press(start)
        n = len(app.tg.msgs)
        await app.press(start)
        assert "expired" in norm(app.out.last_text)
        assert len(app.tg.msgs) == n
        await app.db.close()

    asyncio.run(run())


def test_an_unfinished_repost_is_offered_again_and_blocks_a_second_one(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await pick(app)
        await app.press(app.out.callback_data("Start copying"))
        mid = migration_id(app)
        # the copy is finished but the old posts are not decided yet: /repost shows where things stand
        await app.db.set_migration_status(mid, "stopped")
        await app.text("/repost")
        text = norm(app.out.last_text)
        assert "Unfinished repost" in text and "stopped" in text and "Continue" in buttons_text(app)
        await app.text(f"/repost @{OLD} @{NEW}")
        await app.press(app.out.callback_data("Test"))
        assert "unfinished repost for this channel" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_continue_after_a_stop_finishes_the_job_without_copying_twice(tmp_path):
    async def run():
        from app.repost_engine import run_repost

        app = await make_app(tmp_path)
        ch = await app.db.get_channel(1)
        await app.db.create_migration(
            "mig1", 1, old=OLD, new=NEW, include_typed=True, include_posts=False, first_id=1, last_id=10, partial=False, user_id=1
        )
        mig = await app.db.get_migration("mig1")
        seen = []

        async def progress(r):
            seen.append(r.copied_units)

        res = await run_repost(app.tg, app.db, ch, mig, 1, delay=0, settle_pause=0, progress=progress, should_stop=lambda: len(seen) >= 3)
        assert res.stopped and res.copied_units == 3
        await app.db.set_migration_status("mig1", "stopped")
        await app.text("/repost")
        assert "Continue" in buttons_text(app)
        await app.press(app.out.callback_data("Continue"))
        assert "all posts copied in order" in norm(app.out.last_text)
        texts = [m.message for i, m in sorted(app.tg.msgs.items()) if i > 10]
        assert len(texts) == 8 and texts.count("Welcome") == 1
        await app.db.close()

    asyncio.run(run())


def test_stop_button_without_a_running_job(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.press("rps:nothing")
        assert "Nothing is running" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


def test_a_failed_copy_is_reported_and_can_be_retried(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        app.tg.drop_all_markup = True  # Telegram never shows the keyboards
        await copy_all(app)
        text = norm(app.out.last_text)
        assert "not everything was copied" in text
        assert "2 post(s) failed" in text and "ButtonsNotShown" in text
        assert "Try the rest again" in buttons_text(app) and "Delete the old posts" not in buttons_text(app)
        # the failed posts left no copy behind
        assert not [m for i, m in app.tg.msgs.items() if i > 10 and m.message in ("Movie",)]
        # once Telegram behaves, "try the rest again" completes the job
        app.tg.drop_all_markup = False
        await app.press(app.out.callback_data("Try the rest again"))
        assert "all posts copied in order" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(run())


# ----------------------------------------------------------------------------- deleting old posts
def test_delete_old_posts_with_the_bot_alone(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        mid = migration_id(app)
        await app.press(app.out.callback_data("Delete the old posts"))
        text = norm(app.out.last_text)
        assert "Delete 8 old message(s)" in text and "can't be undone" in text
        await app.press(app.out.callback_data("Yes, delete 8"))
        text = norm(app.out.last_text)
        assert "Done. 8 old message(s) deleted" in text and "by the userbot" not in text
        assert sorted(app.tg.msgs) == [6] + list(range(11, 19))
        assert (await app.db.get_migration(mid)).status == "old_deleted"
        assert await app.db.open_migration() is None
        # the copies are the channel now: still registered in My posts, the originals are not
        assert await app.db.find_by_message(1, 12) is not None and await app.db.find_by_message(1, 2) is None
        await app.db.close()

    asyncio.run(run())


def test_old_posts_the_bot_cannot_delete_are_explained_when_there_is_no_userbot(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        app.tg.undeletable = {1, 2, 3, 4, 5}  # "older than 48 hours" for the bot
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        text = norm(app.out.last_text)
        assert "Deleted 3, 5 old message(s) are still there" in text
        assert "userbot" in text and "/userbot" in text
        assert "Try again" in buttons_text(app) and "Close this repost" in buttons_text(app)
        assert sorted(i for i in app.tg.msgs if i <= 10) == [1, 2, 3, 4, 5, 6]
        await app.db.close()

    asyncio.run(run())


def userbot_setup(app, *, bot_rights=None, username="helper_acct"):
    """A real Userbot object wired to a simulated second account, plus the channel rights of the bot."""
    app.tg.rights = bot_rights or dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=True)
    app.tg.username = None  # a private channel: the only way in is an invite link
    holder = {}

    def factory(cfg):
        holder["client"] = FakeUserClient(app.tg, username=username)
        return holder["client"]

    cfg = SimpleNamespace(userbot_session="SESSION", userbot_keep_admin=False, api_id=1, api_hash="x")
    ub = Userbot(app.tg, cfg, client_factory=factory)
    app.ctx.userbot = ub
    return ub, holder


def test_the_userbot_joins_promotes_deletes_and_then_steps_back(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        ub, holder = userbot_setup(app)
        app.tg.undeletable = {1, 2, 3, 4, 5}
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        text = norm(app.out.last_text)
        assert "Done. 8 old message(s) deleted (5 of them by the userbot)" in text
        assert "made a one-time invite link" in text and "the userbot joined the channel" in text
        assert "made the userbot an admin with only “Delete messages”" in text
        assert "took the admin right away from the userbot again" in text
        assert sorted(app.tg.msgs) == [6] + list(range(11, 19))
        # the userbot only did what the bot could not
        assert holder["client"].deleted == [[1, 2, 3, 4, 5]]
        # nothing is left behind: no admin right, and the temporary link is revoked
        assert app.tg.admins == {}
        assert all(v["revoked"] for v in app.tg.invites.values()) and len(app.tg.invites) == 1
        # the user's account stays a member (it was never asked to leave)
        assert 777 in app.tg.members
        await app.db.close()

    asyncio.run(run())


def test_the_userbot_is_not_touched_when_the_bot_can_delete_everything(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        ub, holder = userbot_setup(app)
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        assert "Done. 8 old message(s) deleted" in norm(app.out.last_text)
        assert "by the userbot" not in norm(app.out.last_text)
        # the helper account is only logged in; it joined nothing and did nothing
        assert holder["client"].requests == [] and holder["client"].deleted == []
        assert app.tg.invites == {} and app.tg.members == set() and app.tg.admins == {}
        await app.db.close()

    asyncio.run(run())


def test_without_the_add_admins_right_the_owner_is_told_what_to_do(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        userbot_setup(app, bot_rights=dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=False))
        app.tg.undeletable = {1, 2, 3, 4, 5}
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        text = norm(app.out.last_text)
        assert "Deleted 3, 5 old message(s) are still there" in text
        assert "Add new admins" in text
        assert "Try again" in buttons_text(app)
        assert app.tg.admins == {}
        assert all(v["revoked"] for v in app.tg.invites.values())  # the link made for the userbot does not stay open
        await app.db.close()

    asyncio.run(run())


def test_try_again_after_fixing_the_rights_finishes_the_job(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        userbot_setup(app, bot_rights=dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=False))
        app.tg.undeletable = {1, 2, 3, 4, 5}
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        assert "Try again" in buttons_text(app)
        app.tg.rights = dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=True)  # the owner fixed it
        await app.press(app.out.callback_data("Try again"))
        text = norm(app.out.last_text)
        assert "Done. 5 old message(s) deleted (5 of them by the userbot)" in text
        assert sorted(app.tg.msgs) == [6] + list(range(11, 19))
        await app.db.close()

    asyncio.run(run())


def test_close_this_repost_after_deleting_by_hand(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        mid = migration_id(app)
        app.tg.undeletable = {1, 2, 3, 4, 5}
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        await app.press(app.out.callback_data("Close this repost"))
        assert "Closed" in norm(app.out.last_text)
        assert (await app.db.get_migration(mid)).status == "closed"
        assert await app.db.open_migration() is None
        await app.db.close()

    asyncio.run(run())


# ----------------------------------------------------------------------------------------- undo
def test_undo_removes_the_copies_and_keeps_the_originals(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        await app.press(app.out.callback_data("Undo"))
        assert "Remove the 8 copied message(s)" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Yes, remove the copies"))
        assert "8 copied message(s) removed" in norm(app.out.last_text)
        assert sorted(app.tg.msgs) == sorted(build())
        await app.db.close()

    asyncio.run(run())


def test_undo_is_not_offered_once_originals_are_gone(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await copy_all(app)
        mid = migration_id(app)
        await app.press(app.out.callback_data("Delete the old posts"))
        await app.press(app.out.callback_data("Yes, delete"))
        await app.press(f"rpu:{mid}")
        assert "Undo is not possible" in norm(app.out.last_text) or "already deleted" in norm(app.out.last_text)
        await app.press(f"rpuy:{mid}")
        assert "Undo is not possible" in norm(app.out.last_text)
        assert [i for i in app.tg.msgs if i > 10] == list(range(11, 19))  # the copies are safe
        await app.db.close()

    asyncio.run(run())



def test_trial_run_can_be_removed_or_kept(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        await app.text(f"/repost @{OLD} @{NEW} --last 3")
        await app.press(app.out.callback_data("Test"))
        assert "Trial run" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Start copying"))
        text = norm(app.out.last_text)
        assert "trial finished" in text and "Delete the old posts" not in buttons_text(app)
        mid = migration_id(app)
        close = app.out.callback_data("Close, keep the copies")
        # a trial never offers to delete the old posts, even by a hand-made button press
        await app.press(f"rpd:{mid}")
        assert "complete, successful copy" in norm(app.out.last_text)
        await app.press(f"rpdy:{mid}")
        assert "complete, successful copy" in norm(app.out.last_text)
        assert OLD_IDS <= set(app.tg.msgs)  # nothing was deleted
        await app.press(close)
        assert "Closed" in norm(app.out.last_text)
        assert await app.db.open_migration() is None
        await app.db.close()

    asyncio.run(run())


def test_messages_posted_during_the_plan_are_not_part_of_it(tmp_path):
    async def run():
        msgs = {i: make_msg(i, f"post {i}") for i in range(1, 6)}
        app = await make_app(tmp_path, msgs=msgs)
        await pick(app)
        start = app.out.callback_data("Start copying")
        app.tg.msgs[6] = make_msg(6, "posted while the plan was open")  # a new post appears
        app.tg._top = 6
        await app.press(start)
        copies = [m.message for i, m in sorted(app.tg.msgs.items()) if i > 6]
        assert copies == [f"post {i}" for i in range(1, 6)]  # exactly what the plan showed
        await app.db.close()

    asyncio.run(run())
