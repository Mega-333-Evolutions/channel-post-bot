"""The userbot: logging in, joining a channel by invite link, getting the "Delete messages" right, deleting, cleaning up."""
import asyncio
from types import SimpleNamespace

import pytest
from telethon import functions, types

from app.userbot import Userbot, UserbotError, delete_problem_text, parse_invite_hash

from .fakes import FakeChannelClient, FakeUserClient, make_msg, rpc

FULL = dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=True)


def run(coro):
    return asyncio.run(coro)


def channel(n=30, **kw):
    bot = FakeChannelClient({i: make_msg(i, f"post {i}") for i in range(1, n + 1)}, username=kw.pop("username", None))
    bot.rights = dict(kw.pop("rights", FULL))
    return bot


def ch_row(bot):
    return SimpleNamespace(id=1, access_hash=5, title="Test", username=bot.username)


def make_userbot(bot, *, keep_admin=False, session="SESSION", user=None, **user_kw):
    made = {}

    def factory(cfg):
        made["client"] = user or FakeUserClient(bot, **user_kw)
        return made["client"]

    cfg = SimpleNamespace(userbot_session=session, userbot_keep_admin=keep_admin, api_id=1, api_hash="x")
    return Userbot(bot, cfg, client_factory=factory), made


# ------------------------------------------------------------------------------------------ helpers
def test_parse_invite_hash():
    assert parse_invite_hash("https://t.me/+AbCdEfGh0001") == "AbCdEfGh0001"
    assert parse_invite_hash("t.me/+AbCdEfGh0001") == "AbCdEfGh0001"
    assert parse_invite_hash("https://t.me/joinchat/AbCdEfGh-_01") == "AbCdEfGh-_01"
    assert parse_invite_hash("tg://join?invite=AbCdEfGh0001") == "AbCdEfGh0001"
    assert parse_invite_hash("https://telegram.me/+AbCdEfGh0001?x=1") == "AbCdEfGh0001"
    assert parse_invite_hash("https://t.me/somechannel") is None
    assert parse_invite_hash("") is None and parse_invite_hash(None) is None


# --------------------------------------------------------------------------------------- logging in
def test_not_set_up():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot, session="")
        assert not ub.enabled and not ub.ready
        assert await ub.connect() is False and "USERBOT_SESSION is not set" in ub.last_error
        assert "not set up" in ub.describe()
        assert await ub.fallback_for(ch_row(bot)) == (None, None)
        assert made == {}  # nothing was even created

    run(go())


def test_connect_ok_and_describe():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot, username="helper_acct")
        assert await ub.connect() is True and ub.ready and made["client"].connected
        assert ub.describe() == "connected as @helper_acct (id 777)"
        assert await ub.connect() is True  # a second call does nothing
        fb, deleter = await ub.fallback_for(ch_row(bot))
        assert fb == deleter.delete and bot.requests == []  # nothing happens in the channel until it is needed
        await ub.close()
        assert not ub.ready and not made["client"].connected

    run(go())


def test_connect_without_a_username_describes_the_name():
    async def go():
        bot = channel()
        ub, _ = make_userbot(bot, username=None, first_name="Helper")
        await ub.connect()
        assert ub.describe() == "connected as Helper (id 777)"

    run(go())


def test_a_revoked_session_is_reported():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot, authorized=False)
        assert await ub.connect() is False and not ub.ready
        assert "not logged in any more" in ub.last_error and "make_userbot_session.py" in ub.last_error
        assert not made["client"].connected  # it hangs up again
        assert "not connected" in ub.describe() and "not logged in" in ub.describe()
        assert await ub.fallback_for(ch_row(bot)) == (None, None)

    run(go())


def test_a_bot_session_is_refused():
    async def go():
        bot = channel()
        ub, _ = make_userbot(bot, user=FakeUserClient(bot, bot=True))
        assert await ub.connect() is False and "belongs to a bot" in ub.last_error

    run(go())


def test_a_connection_error_is_not_fatal():
    async def go():
        bot = channel()

        class Broken(FakeUserClient):
            async def connect(self):
                raise ConnectionError("no route to host")

        ub, _ = make_userbot(bot, user=Broken(bot))
        assert await ub.connect() is False and "ConnectionError: no route to host" in ub.last_error

    run(go())


# ------------------------------------------------------------------------------------------ deleting
async def ready_deleter(bot, **kw):
    ub, made = make_userbot(bot, **kw)
    assert await ub.connect()
    return ub, made, ub.deleter(ch_row(bot))


def test_private_channel_one_time_link_then_admin_then_clean_up():
    async def go():
        bot = channel()
        ub, made, d = await ready_deleter(bot)
        await d.delete([1, 2, 3])
        user = made["client"]
        assert sorted(bot.msgs)[:3] == [4, 5, 6] and user.deleted == [[1, 2, 3]]
        # the link was made by the bot with a limit of one use and an expiry, and the userbot joined with it
        (h, inv), = bot.invites.items()
        assert inv["limit"] == 1 and inv["title"] == "Cleanup helper" and inv["expire"] is not None
        assert inv["uses"] == 1 and 777 in bot.members
        # promoted with exactly one right
        rights = bot.admins[777]
        assert rights.delete_messages and not any(
            v for k, v in rights.to_dict().items() if k not in ("_", "delete_messages")
        )
        assert d.notes == [
            "made a one-time invite link for the userbot",
            "the userbot joined the channel",
            "made the userbot an admin with only “Delete messages”",
        ]
        await d.close()
        assert bot.admins == {} and inv["revoked"] is True
        assert d.notes[-1] == "took the admin right away from the userbot again"
        assert not ub.lock.locked()

    run(go())


def test_the_second_delete_reuses_the_prepared_channel():
    async def go():
        bot = channel()
        ub, made, d = await ready_deleter(bot)
        await d.delete([1, 2])
        n = len(made["client"].requests)
        await d.delete([3, 4])
        # only the delete request was added: no second join, no second promotion
        assert [type(r).__name__ for r in made["client"].requests[n:]] == ["DeleteMessagesRequest"]
        assert len(bot.invites) == 1
        await d.close()

    run(go())


def test_more_than_100_ids_are_sent_in_chunks_of_100():
    async def go():
        bot = channel(250)
        ub, made, d = await ready_deleter(bot)
        await d.delete(list(range(1, 251)))
        await d.close()
        assert [len(x) for x in made["client"].deleted] == [100, 100, 50] and bot.msgs == {}

    run(go())


def test_channels_own_invite_link_is_used_and_never_revoked():
    async def go():
        bot = channel(rights=dict(FULL, invite=False))  # the bot may not make links itself
        bot.primary_invite = "https://t.me/+PrimaryLink01"
        bot.invites["PrimaryLink01"] = dict(limit=0, uses=0, revoked=False, title=None, expire=None)
        ub, made, d = await ready_deleter(bot)
        await d.delete([1])
        await d.close()
        assert "used the channel's own invite link" in d.notes
        assert bot.invites["PrimaryLink01"]["revoked"] is False  # it is not ours to revoke
        assert 1 not in bot.msgs

    run(go())


def test_public_channel_is_joined_by_its_name_when_there_is_no_link():
    async def go():
        bot = channel(username="testch", rights=dict(FULL, invite=False))
        ub, made, d = await ready_deleter(bot)
        await d.delete([1, 2])
        await d.close()
        assert "the userbot joined the channel by its public name" in d.notes
        assert any(isinstance(r, functions.channels.JoinChannelRequest) for r in made["client"].requests)
        assert 1 not in bot.msgs and 2 not in bot.msgs

    run(go())


def test_private_channel_without_any_link_explains_what_to_do():
    async def go():
        bot = channel(rights=dict(FULL, invite=False))
        ub, made, d = await ready_deleter(bot)
        with pytest.raises(UserbotError) as e:
            await d.delete([1])
        assert "Invite users via link" in str(e.value) and "add the userbot to the channel yourself" in str(e.value)
        await d.close()
        assert not ub.lock.locked()

    run(go())


def test_an_expired_link_falls_back_to_the_public_name():
    async def go():
        bot = channel(username="testch")
        ub, made, d = await ready_deleter(bot)
        bot.fail["ExportChatInviteRequest"] = rpc("ChatAdminRequiredError")
        bot.primary_invite = "https://t.me/+GoneGone0001"  # not in bot.invites: Telegram says the hash is invalid
        await d.delete([1])
        await d.close()
        assert "the userbot joined the channel by its public name" in d.notes

    run(go())


def test_a_channel_that_wants_to_approve_new_members():
    async def go():
        bot = channel()
        bot.require_approval = True
        ub, made, d = await ready_deleter(bot)
        with pytest.raises(UserbotError) as e:
            await d.delete([1])
        assert "approve" in str(e.value)
        await d.close()
        assert all(v["revoked"] for v in bot.invites.values())  # the link made for it does not stay around

    run(go())


@pytest.mark.parametrize(
    "error,text",
    [
        ("ChannelsTooMuchError", "too many channels"),
        ("UserChannelsTooMuchError", "too many channels"),
        ("UserBannedInChannelError", "banned in this channel"),
    ],
)
def test_join_problems_are_explained(error, text):
    async def go():
        bot = channel()
        bot.fail["user:ImportChatInviteRequest"] = rpc(error)
        ub, made, d = await ready_deleter(bot)
        with pytest.raises(UserbotError) as e:
            await d.delete([1])
        assert text in str(e.value)
        await d.close()

    run(go())


def test_userbot_already_in_the_channel_as_a_plain_member():
    async def go():
        bot = channel()
        ub, made, d = await ready_deleter(bot)
        made["client"]._join()  # a member from an earlier clean-up
        await d.delete([1])
        await d.close()
        assert not any(isinstance(r, functions.messages.ImportChatInviteRequest) for r in made["client"].requests)
        assert "the userbot joined the channel" not in d.notes and bot.invites == {}
        assert 1 not in bot.msgs

    run(go())


def test_userbot_that_may_already_delete_is_left_alone():
    async def go():
        bot = channel(rights=dict(FULL, invite=False, add_admins=False))
        ub, made, d = await ready_deleter(bot)
        made["client"]._join()
        bot.admins[777] = types.ChatAdminRights(delete_messages=True)  # the owner made it an admin by hand
        await d.delete([1, 2])
        await d.close()
        assert bot.admins[777].delete_messages  # still an admin: we did not make it one, so we do not take it away
        assert d.notes == [] and bot.msgs.get(1) is None

    run(go())


def test_userbot_may_not_delete_and_the_bot_cannot_promote_it():
    async def go():
        bot = channel(rights=dict(FULL, add_admins=False))
        ub, made, d = await ready_deleter(bot)
        with pytest.raises(UserbotError) as e:
            await d.delete([1])
        assert "Add new admins" in str(e.value)
        await d.close()
        assert bot.admins == {} and 1 in bot.msgs

    run(go())


def test_promotion_refused_by_telegram():
    async def go():
        bot = channel()
        bot.fail["EditAdminRequest"] = rpc("RightForbiddenError")
        ub, made, d = await ready_deleter(bot)
        with pytest.raises(UserbotError) as e:
            await d.delete([1])
        assert "RightForbiddenError" in str(e.value) and "may not give that right" in str(e.value)
        await d.close()
        assert bot.admins == {}

    run(go())


def test_the_bot_cannot_find_the_account_to_promote():
    async def go():
        bot = channel()
        bot.participants_visible = False
        ub, made, d = await ready_deleter(bot)
        with pytest.raises(UserbotError) as e:
            await d.delete([1])
        assert "cannot find that account" in str(e.value)
        await d.close()

    run(go())


def test_the_bot_knows_the_account_already():
    async def go():
        bot = channel()
        ub, made, d = await ready_deleter(bot)
        bot.known_users[777] = made["client"].me  # the bot has met this user before
        bot.participants_visible = False
        await d.delete([1])
        await d.close()
        assert 1 not in bot.msgs and bot.admins == {}

    run(go())


def test_keep_admin_keeps_the_right():
    async def go():
        bot = channel()
        ub, made, d = await ready_deleter(bot, keep_admin=True)
        await d.delete([1])
        await d.close()
        assert bot.admins[777].delete_messages  # USERBOT_KEEP_ADMIN=true
        assert not any("took the admin right away" in n for n in d.notes)
        assert next(iter(bot.invites.values()))["revoked"] is True  # the link is revoked either way

    run(go())


def test_failing_demotion_is_reported_not_fatal():
    async def go():
        bot = channel()
        ub, made, d = await ready_deleter(bot)
        await d.delete([1])
        bot.fail["EditAdminRequest"] = rpc("ChatAdminRequiredError")
        await d.close()
        assert d.notes[-1] == "could not take the admin right away from the userbot - please do it by hand"
        assert not ub.lock.locked()

    run(go())


def test_not_connected_userbot_cannot_delete():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot, authorized=False)
        await ub.connect()
        d = ub.deleter(ch_row(bot))
        with pytest.raises(UserbotError) as e:
            await d.delete([1])
        assert "not connected" in str(e.value)

    run(go())


def test_clean_ups_of_two_channels_do_not_overlap():
    async def go():
        bot = channel()
        ub, made = make_userbot(bot)
        await ub.connect()
        first, second = ub.deleter(ch_row(bot)), ub.deleter(ch_row(bot))
        await first.delete([1])
        order = []

        async def other():
            await second.delete([2])  # waits until the first clean-up is closed
            order.append("second")
            await second.close()

        t = asyncio.create_task(other())
        await asyncio.sleep(0.1)
        assert order == [] and ub.lock.locked()
        order.append("first closes")
        await first.close()
        await asyncio.wait_for(t, 5)
        assert order == ["first closes", "second"] and not ub.lock.locked()

    run(go())


def test_a_failed_prepare_can_be_retried_without_a_deadlock():
    async def go():
        bot = channel(rights=dict(FULL, invite=False))
        ub, made, d = await ready_deleter(bot)
        with pytest.raises(UserbotError):
            await d.delete([1])  # no way into the channel
        bot.rights = dict(FULL)  # the owner fixed the rights
        await asyncio.wait_for(d.delete([1]), 5)  # the same deleter tries again; it must not wait for its own lock
        await d.close()
        assert 1 not in bot.msgs and not ub.lock.locked()

    run(go())


def test_close_without_anything_done_is_harmless():
    async def go():
        bot = channel()
        ub, made, d = await ready_deleter(bot)
        await d.close()
        await d.close()
        assert bot.requests == [] and not ub.lock.locked()

    run(go())


# -------------------------------------------------------------------------------- words for the owner
def test_delete_problem_text():
    off = SimpleNamespace(enabled=False)
    ready = SimpleNamespace(enabled=True, ready=True, last_error=None)
    down = SimpleNamespace(enabled=True, ready=False, last_error="the session was revoked")
    assert "see /userbot" in delete_problem_text(None)
    assert "see /userbot" in delete_problem_text(off)
    assert "the session was revoked" in delete_problem_text(down)
    assert "tried as well" in delete_problem_text(ready, tried=True)
    assert "The userbot could not help: boom" in delete_problem_text(ready, fallback_error="boom", tried=True)
    t = delete_problem_text(ready, bot_error="ChatAdminRequiredError", tried=True)
    assert t.startswith("Telegram refused the bot (ChatAdminRequiredError).")
    assert "age limit" in delete_problem_text(ready)  # nothing else to say: a generic hint
