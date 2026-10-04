"""The repost / delete / buttons / userbot flows through a REAL Telethon client object.

Telethon resolves and serialises every request exactly as it does before sending it to Telegram; only the network at
the very end is replaced by the simulated channel. A request that would not build or serialise in production fails here.
"""
import asyncio
import datetime

import pytest
from telethon import TelegramClient, functions, types, utils
from telethon.sessions import MemorySession

from app.buttons_sync import ensure_markup, fix_buttons, scan_buttons
from app.repost_engine import RepostOptions, delete_copies, plan_repost, run_repost
from app.tgutil import build_markup, button_url, get_rights, peer_of
from app.userbot import Userbot

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, FakeUserClient, make_msg, markup, url_btn
from .test_repost import NEW, OLD, build


@pytest.fixture()
def wired(monkeypatch):
    chan = FakeChannelClient(build())
    chan.rights = dict(admin=True, post=True, edit=True, delete=True, invite=True, add_admins=True)
    seen = []

    async def fake_call(self, request, ordered=False, flood_sleep_threshold=None):
        assert not isinstance(request, (list, tuple))
        await request.resolve(self, utils)  # what Telethon does just before sending
        seen.append(type(request).__name__)
        return await chan(request)  # serialises again and answers like Telegram

    monkeypatch.setattr(TelegramClient, "__call__", fake_call)
    return TelegramClient(MemorySession(), 1, "h"), chan, seen


async def setup(tmp_path, client):
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Test", "testch", 99)
    ch = await db.get_channel(1)
    plan = await plan_repost(client, ch, RepostOptions(OLD, NEW))
    assert plan.error is None
    await db.create_migration(
        "m1", 1, old=OLD, new=NEW, include_typed=True, include_posts=False, first_id=plan.first, last_id=plan.last, partial=False, user_id=99
    )
    return db, ch, await db.get_migration("m1")


def test_repost_then_delete_through_a_real_client(wired, tmp_path):
    client, chan, seen = wired

    async def go():
        db, ch, mig = await setup(tmp_path, client)
        res = await run_repost(client, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.copied_messages, res.failed, res.aborted) == (7, 8, [], None)
        assert (res.forwarded, res.rebuilt) == (3, 4)
        new = [chan.msgs[i] for i in sorted(chan.msgs) if i > 10]
        assert len(new) == 8 and new[0].message == "Welcome"
        assert button_url(new[1].reply_markup.rows[0].buttons[0]) == f"https://t.me/{NEW}?start=AAA"
        out = await delete_copies(client, db, ch, mig, "old", delay=0)
        assert (out.deleted, out.remaining, out.error) == (8, 0, None)
        assert sorted(chan.msgs) == [6] + list(range(11, 19))
        await db.close()

    asyncio.run(go())
    assert {"GetFullChannelRequest", "GetMessagesRequest", "ForwardMessagesRequest", "SendMessageRequest", "SendMediaRequest", "DeleteMessagesRequest"} <= set(seen)


def test_a_channel_that_forbids_forwarding_rebuilds_albums_through_a_real_client(wired, tmp_path):
    client, chan, seen = wired
    chan.restrict_forwards = True

    async def go():
        db, ch, mig = await setup(tmp_path, client)
        res = await run_repost(client, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.forwarded, res.rebuilt) == (7, [], 0, 7)
        new = {i: m for i, m in chan.msgs.items() if i > 10}
        assert new[14].grouped_id == new[15].grouped_id and new[14].media.photo.id == 41
        await db.close()

    asyncio.run(go())
    assert "SendMultiMediaRequest" in seen


def test_the_repair_edits_go_through_a_real_client(wired, tmp_path):
    client, chan, seen = wired
    chan.hide_keyboard_on_create = 1

    async def go():
        db, ch, mig = await setup(tmp_path, client)
        res = await run_repost(client, db, ch, mig, 99, delay=0, settle_pause=0)
        assert res.failed == [] and res.repaired == 1
        await db.close()

    asyncio.run(go())
    # the plain edit (Telegram answers "not modified"), then the stand-in keyboard, then the real one
    assert seen.count("EditMessageRequest") == 3 and len(chan.edits) == 2


def test_checking_and_fixing_buttons_through_a_real_client(wired, tmp_path):
    client, chan, seen = wired
    A, B = "https://t.me/goku?start=A", "https://t.me/goku?start=B"
    chan.msgs = {
        1: make_msg(1, "fine", markup=markup([url_btn("Get", A)]), out=True),
        2: make_msg(2, "lost its buttons", out=True),
        3: make_msg(3, "wrong link", markup=markup([url_btn("Get", B)]), out=True),
    }
    chan._top = 3

    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Test", "testch", 99)
        ch = await db.get_channel(1)
        posts = [
            await db.create_post(channel_id=1, message_id=i, status="sent", source="bot", text=f"p{i}", entities=[], buttons=[[{"t": "Get", "u": A}]])
            for i in (1, 2, 3)
        ]
        scan = await scan_buttons(client, ch, posts)
        assert (scan.total, scan.fine, [p.message_id for p, _, _ in scan.bad], scan.gone) == (3, 1, [2, 3], [])
        res = await fix_buttons(client, ch, scan.bad, delay=0, pause=0)
        assert (res.fixed, res.failed) == (2, [])
        assert [button_url(chan.msgs[i].reply_markup.rows[0].buttons[0]) for i in (1, 2, 3)] == [A, A, A]
        assert await ensure_markup(client, peer_of(ch), 2, build_markup([[{"t": "Get", "u": A}]]), pause=0) == "fine"
        await db.close()

    asyncio.run(go())
    assert "EditMessageRequest" in seen


def test_the_bots_own_rights_are_read_through_a_real_client(wired):
    client, chan, seen = wired

    async def go():
        ch = type("Ch", (), {"id": 1, "access_hash": 5})()
        r = await get_rights(client, ch)
        assert (r.admin, r.post, r.delete, r.invite, r.add_admins) == (True, True, True, True, True)
        chan.rights = dict(admin=True, post=True, edit=False, delete=False, invite=False, add_admins=False)
        r = await get_rights(client, ch)
        assert (r.post, r.delete, r.invite, r.add_admins) == (True, False, False, False)

    asyncio.run(go())
    assert seen == ["GetParticipantRequest", "GetParticipantRequest"]


def test_userbot_clean_up_with_a_real_bot_client(wired, tmp_path):
    """Bot side (invite link, promotion, demotion, revoking) runs through the real client; the helper account is simulated."""
    client, chan, seen = wired
    chan.undeletable = {1, 2, 3, 4, 5}

    class Cfg:
        userbot_session, userbot_keep_admin, api_id, api_hash = "S", False, 1, "h"

    async def go():
        db, ch, mig = await setup(tmp_path, client)
        await run_repost(client, db, ch, mig, 99, delay=0, settle_pause=0)
        ub = Userbot(client, Cfg(), client_factory=lambda cfg: FakeUserClient(chan))
        fallback, deleter = await ub.fallback_for(ch)
        try:
            out = await delete_copies(client, db, ch, mig, "old", delay=0, fallback=fallback)
        finally:
            await deleter.close()
        assert (out.deleted, out.by_userbot, out.remaining, out.error, out.fallback_error) == (8, 5, 0, None, None)
        assert chan.admins == {} and all(v["revoked"] for v in chan.invites.values())
        await db.close()

    asyncio.run(go())
    for name in ("ExportChatInviteRequest", "EditAdminRequest", "GetParticipantsRequest", "EditExportedChatInviteRequest"):
        assert name in seen, (name, seen)
    assert seen.count("EditAdminRequest") == 2  # promote, then demote


def test_invite_and_admin_requests_build_with_the_values_the_userbot_code_uses(wired):
    client, chan, seen = wired
    peer = types.InputPeerChannel(1, 5)
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    async def go():
        inv = await client(functions.messages.ExportChatInviteRequest(peer=peer, expire_date=expires, usage_limit=1, title="Cleanup helper"))
        assert inv.link.startswith("https://t.me/+") and inv.usage_limit == 1
        await client(functions.messages.EditExportedChatInviteRequest(peer=peer, link=inv.link, revoked=True))
        chan.users[777] = types.User(id=777, access_hash=555, first_name="H")
        await client(
            functions.channels.EditAdminRequest(
                channel=peer, user_id=types.InputUser(777, 555), admin_rights=types.ChatAdminRights(delete_messages=True), rank=""
            )
        )
        assert chan.admins[777].delete_messages
        await client(
            functions.channels.EditAdminRequest(channel=peer, user_id=types.InputUser(777, 555), admin_rights=types.ChatAdminRights(), rank="")
        )
        assert chan.admins == {}
        res = await client(
            functions.channels.GetParticipantsRequest(channel=peer, filter=types.ChannelParticipantsSearch("h"), offset=0, limit=200, hash=0)
        )
        assert res.users == []

    asyncio.run(go())
