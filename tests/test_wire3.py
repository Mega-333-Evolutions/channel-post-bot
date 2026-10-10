"""Replies, pins, the copy between two channels and the sync with the channels, through a REAL Telethon client object.

Telethon resolves and serialises every request exactly as it does before sending it to Telegram; only the network at
the very end is replaced by the simulated channels. A request that would not build or serialise in production fails here.
"""
import asyncio

import pytest
from telethon import TelegramClient, functions, types, utils
from telethon.sessions import MemorySession

from app.db import utcnow
from app.repost_engine import run_repost
from app.servicemsg import remove
from app.shift_engine import delete_shift_copies, run_shift
from app.sync_engine import SyncOptions, sync_channel, sync_ids
from app.tgutil import peer_of

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, markup, rpc, service_msg, url_btn
from .test_repost_links import CH, build_linked, new_posts, reply_of
from .test_shift import SRC, old_posts, source_posts
from .test_sync import foreign, old_msg, saved


class Wire:
    """A real client whose requests are answered by `root` (a simulated channel; the other channels sit next to it)."""

    def __init__(self):
        self.client = TelegramClient(MemorySession(), 1, "h")
        self.root = None
        self.seen = []


@pytest.fixture()
def wire(monkeypatch):
    w = Wire()

    async def fake_call(self, request, ordered=False, flood_sleep_threshold=None):
        assert not isinstance(request, (list, tuple))
        await request.resolve(self, utils)  # what Telethon does just before sending
        w.seen.append(request)
        return await w.root(request)  # serialises again and answers like Telegram

    monkeypatch.setattr(TelegramClient, "__call__", fake_call)
    return w


def two_wired(w, *, restrict=False):
    """channel 1 "srcch" with the source posts, channel 2 "dstch" with ten old posts."""
    src = FakeChannelClient(source_posts(), username="srcch", title="Anime Source")
    src.restrict_forwards = restrict
    dst = src.add_channel(FakeChannelClient(old_posts(), username="dstch", cid=2, title="Anime Backup"))
    w.root = src
    return src, dst


def names(seen):
    return [type(r).__name__ for r in seen]


def test_replies_and_pins_of_a_repost_go_through_a_real_client(wire, tmp_path):
    client, seen = wire.client, wire.seen
    chan = wire.root = FakeChannelClient(build_linked(), username=CH)

    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Test", CH, 99)
        ch = await db.get_channel(1)
        await db.create_migration("m1", 1, old=None, new=None, include_typed=False, include_posts=False, first_id=1, last_id=10, partial=False, user_id=99)
        mig = await db.get_migration("m1")
        res = await run_repost(client, db, ch, mig, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.aborted) == (8, [], None)
        new = new_posts(chan)
        assert (reply_of(new[14]), reply_of(new[15]), reply_of(new[16]), reply_of(new[17])) == (13, 14, 15, 15)
        assert (res.final.pin_wanted, res.final.pinned, res.final.pin_error) == (2, 2, None)
        assert chan.pinned_order == [15, 19] and new[15].pinned and new[19].pinned
        assert not [m for m in chan.msgs.values() if isinstance(m, types.MessageService) and m.id > 10]  # pin notices deleted
        await db.close()

    asyncio.run(go())
    got = names(seen)
    for name in ("SendMessageRequest", "SendMultiMediaRequest", "UpdatePinnedMessageRequest", "DeleteMessagesRequest", "EditMessageRequest"):
        assert name in got, (name, got)
    # an answer is made with the new-style reply header, never with the old reply_to_msg_id field
    replies = [r.reply_to for r in seen if isinstance(r, (functions.messages.SendMessageRequest, functions.messages.SendMultiMediaRequest)) and r.reply_to]
    assert replies and all(isinstance(r, types.InputReplyToMessage) for r in replies)
    pins = [r for r in seen if isinstance(r, functions.messages.UpdatePinnedMessageRequest)]
    assert [(p.id, p.silent, bool(p.unpin)) for p in pins] == [(15, True, False), (19, True, False)]


def test_a_shift_between_two_channels_through_a_real_client(wire, tmp_path):
    client, seen = wire.client, wire.seen
    src, dst = two_wired(wire)

    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(2, 6, "Anime Backup", "dstch", 1)
        dst_row = await db.get_channel(2)
        await db.create_shift("s1", src=SRC, dst_channel_id=2, first_id=1, last_id=8, user_id=1)
        shift = await db.get_shift("s1")
        res = await run_shift(client, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.copied_messages, res.failed, res.aborted) == (6, 7, [], None)
        new = {i: m for i, m in dst.msgs.items() if i > 10}
        assert sorted(new) == [11, 12, 13, 14, 15, 16, 17]
        assert reply_of(new[14]) == 12 and not new[14].pinned
        assert new[17].message == "See https://t.me/dstch/11 and https://t.me/dstch/12 and https://t.me/elsewhere/9"
        assert sorted(src.msgs) == [1, 2, 3, 4, 5, 6, 7, 8] and src.msgs[5].pinned  # the source is only read
        assert await db.shift_pairs("s1") == [(1, 11), (2, 12), (3, 13), (5, 14), (6, 15), (7, 16), (8, 17)]

        undo = await delete_shift_copies(client, db, dst_row, shift, delay=0)
        assert (undo.deleted, undo.remaining, undo.error) == (7, 0, None)
        assert sorted(dst.msgs) == list(range(1, 11))
        assert sorted(src.msgs) == [1, 2, 3, 4, 5, 6, 7, 8]
        await db.close()

    asyncio.run(go())
    # posts are carried over by Telegram itself when it is allowed to (here: from channel 1 to channel 2)
    forwards = [r for r in seen if isinstance(r, functions.messages.ForwardMessagesRequest)]
    assert forwards and all(isinstance(r.from_peer, types.InputPeerChannel) and r.from_peer.channel_id == 1 for r in forwards)
    assert all(isinstance(r.to_peer, types.InputPeerChannel) and r.to_peer.channel_id == 2 for r in forwards)
    assert all(r.drop_author for r in forwards)
    sent = [r for r in seen if isinstance(r, functions.messages.SendMessageRequest) and r.reply_to]
    assert sent and sent[0].peer.channel_id == 2 and sent[0].reply_to.reply_to_msg_id == 12
    # the source channel was never written to
    for r in seen:
        if isinstance(r, (functions.messages.SendMessageRequest, functions.messages.SendMediaRequest, functions.messages.SendMultiMediaRequest, functions.messages.EditMessageRequest)):
            assert r.peer.channel_id == 2
        if isinstance(r, functions.channels.DeleteMessagesRequest):
            assert r.channel.channel_id == 2


def test_a_shift_from_a_channel_that_forbids_forwarding_through_a_real_client(wire, tmp_path):
    client, seen = wire.client, wire.seen
    src, dst = two_wired(wire, restrict=True)

    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(2, 6, "Anime Backup", "dstch", 1)
        dst_row = await db.get_channel(2)
        await db.create_shift("s1", src=SRC, dst_channel_id=2, first_id=1, last_id=8, user_id=1)
        shift = await db.get_shift("s1")
        res = await run_shift(client, db, shift, dst_row, 99, delay=0, settle_pause=0)
        assert (res.copied_units, res.failed, res.forwarded, res.rebuilt) == (6, [], 0, 6)
        new = {i: m for i, m in dst.msgs.items() if i > 10}
        assert new[12].reply_markup is not None and new[15].grouped_id == new[16].grouped_id
        await db.close()

    asyncio.run(go())
    assert "SendMultiMediaRequest" in names(seen) and "SendMediaRequest" in names(seen)


# ===================================================================================== the sync with the channels
def notice(chan, action):
    """A service message as Telegram adds one: it takes the next message id and moves the event counter."""
    chan._top += 1
    chan._pts += 1
    note = types.MessageService(id=chan._top, peer_id=types.PeerChannel(chan.cid), date=utcnow(), action=action)
    chan.msgs[note.id] = note
    return note


def synced_channel(wire):
    """Posts 1, 2, 3 and 5 made through the bot a day ago; 4 is an old "pinned a message" notice."""
    msgs = {
        1: old_msg(1, "first"),
        2: old_msg(2, "second", markup=markup([url_btn("Watch", "https://t.me/a/1")])),
        3: old_msg(3, "third"),
        4: service_msg(4),
        5: old_msg(5, "fifth"),
    }
    chan = wire.root = FakeChannelClient(msgs, username="chan")
    return msgs, chan


async def saved_world(tmp_path, msgs):
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Chan", "chan", 99)
    ch = await db.get_channel(1)
    for m in msgs.values():
        if not isinstance(m, types.MessageService):
            await saved(db, m)
    return db, ch


def test_the_sync_compares_a_channel_and_deletes_new_notices_through_a_real_client(wire, tmp_path):
    client, seen = wire.client, wire.seen
    msgs, chan = synced_channel(wire)

    async def go():
        db, ch = await saved_world(tmp_path, msgs)
        opts = SyncOptions(pause=0, grace=0)

        first = await sync_channel(client, db, ch, opts)  # the first look only notes where the channel stands
        assert (first.error, first.first_look, first.checked, first.changed) == (None, True, 4, False)
        assert sorted(chan.msgs) == [1, 2, 3, 4, 5]

        # what admins do outside the bot: delete a post, edit one, write a new one - Telegram adds notices meanwhile
        del chan.msgs[3]
        chan._pts += 1
        chan.msgs[1].message = "first, edited"
        chan._pts += 1
        foreign(chan, "written by another admin")  # id 6
        notice(chan, types.MessageActionChatEditTitle("A new name"))  # id 7
        notice(chan, types.MessageActionPinMessage())  # id 8

        rep = await sync_channel(client, db, ch, opts)
        assert rep.error is None and rep.anomaly is None
        assert (rep.deleted, rep.edited, rep.adopted, rep.services_deleted, rep.services_failed) == ([3], [1], [6], 2, 0)
        assert sorted(chan.msgs) == [1, 2, 4, 5, 6]  # the two new notices are gone, the old one is left alone

        assert await db.find_by_message(1, 3) is None
        assert (await db.find_by_message(1, 1)).text == "first, edited"
        took = await db.find_by_message(1, 6)
        assert (took.source, took.text, took.created_by, took.status) == ("adopted", "written by another admin", 0, "sent")

        again = await sync_channel(client, db, ch, opts)  # nothing left to do
        assert not again.changed and again.error is None
        await db.close()

    asyncio.run(go())
    got = names(seen)
    for name in ("GetFullChannelRequest", "GetMessagesRequest", "DeleteMessagesRequest"):
        assert name in got, (name, got)
    # the channel counter and every read name the channel the way Telegram wants it
    fulls = [r for r in seen if isinstance(r, functions.channels.GetFullChannelRequest)]
    assert fulls and all(isinstance(r.channel, types.InputChannel) and r.channel.channel_id == 1 for r in fulls)
    reads = [r for r in seen if isinstance(r, functions.channels.GetMessagesRequest)]
    assert reads and all(isinstance(r.channel, types.InputChannel) for r in reads)
    # only the two new notices were deleted, in one request, and nothing was written to the channel
    deletes = [r for r in seen if isinstance(r, functions.channels.DeleteMessagesRequest)]
    assert [i for r in deletes for i in r.id] == [7, 8]
    assert all(isinstance(r.channel, types.InputChannel) and r.channel.channel_id == 1 for r in deletes)
    writes = (
        functions.messages.SendMessageRequest, functions.messages.SendMediaRequest, functions.messages.SendMultiMediaRequest,
        functions.messages.EditMessageRequest, functions.messages.ForwardMessagesRequest, functions.messages.UpdatePinnedMessageRequest,
    )
    assert not [r for r in seen if isinstance(r, writes)]


def test_the_live_look_at_a_few_messages_through_a_real_client(wire, tmp_path):
    client, seen = wire.client, wire.seen
    msgs, chan = synced_channel(wire)

    async def go():
        db, ch = await saved_world(tmp_path, msgs)
        opts = SyncOptions(pause=0, grace=0)
        await sync_channel(client, db, ch, opts)

        chan.msgs[2].message = "second, edited"
        chan._pts += 1
        foreign(chan, "a new post")  # 6
        note = notice(chan, types.MessageActionChatEditTitle("Another name"))  # 7
        del chan.msgs[5]
        chan._pts += 1

        rep = await sync_ids(client, db, ch, [2, 5, 6, note.id], opts, adopt_ids=[6])
        assert rep.error is None
        assert (rep.edited, rep.deleted, rep.adopted, rep.services_deleted) == ([2], [5], [6], 1)
        assert sorted(chan.msgs) == [1, 2, 3, 4, 6]
        assert (await db.find_by_message(1, 2)).text == "second, edited"
        assert (await db.find_by_message(1, 2)).buttons == [[{"t": "Watch", "u": "https://t.me/a/1"}]]  # the saved buttons stay
        assert await db.find_by_message(1, 5) is None
        await db.close()

    asyncio.run(go())
    assert [i for r in seen if isinstance(r, functions.channels.DeleteMessagesRequest) for i in r.id] == [7]


def test_deleting_a_notice_and_the_refusals_through_a_real_client(wire, tmp_path):
    client = wire.client
    msgs, chan = synced_channel(wire)

    async def go():
        db, ch = await saved_world(tmp_path, msgs)
        peer = peer_of(ch)

        out = await remove(client, peer, [4])
        assert (out.deleted, out.failed, out.error) == (1, 0, None) and 4 not in chan.msgs

        chan.fail["DeleteMessagesRequest"] = rpc("ChatAdminRequiredError")
        note = notice(chan, types.MessageActionPinMessage())
        out = await remove(client, peer, [note.id])
        assert (out.deleted, out.failed, out.error_name) == (0, 1, "ChatAdminRequiredError") and note.id in chan.msgs
        chan.fail.clear()

        chan.fail["GetFullChannelRequest"] = rpc("ChannelPrivateError")  # the bot lost its place in the channel
        rep = await sync_channel(client, db, ch, SyncOptions(pause=0, grace=0))
        assert rep.error and not rep.changed and rep.checked == 0
        assert [p.message_id for p in await db.sent_posts(1)] == [1, 2, 3, 5]  # nothing was touched
        await db.close()

    asyncio.run(go())


def test_a_restricted_channel_and_restricted_posts_through_a_real_client(wire, tmp_path):
    """The channel object and the messages come back as real Telethon types, with Telegram's restriction marks."""
    client, seen = wire.client, wire.seen
    msgs, chan = synced_channel(wire)

    async def go():
        db, ch = await saved_world(tmp_path, msgs)
        opts = SyncOptions(pause=0, grace=0)
        await sync_channel(client, db, ch, opts)  # the first look

        notice_text = "This message couldn't be displayed on your device due to copyright infringement."
        chan.restriction = notice_text  # a copyright strike: the channel itself is restricted
        for m in chan.msgs.values():
            if not isinstance(m, types.MessageService):
                m.message = notice_text
        chan._pts += 4
        del chan.msgs[3]
        rep = await sync_channel(client, db, ch, opts)
        assert rep.restricted_channel == notice_text and not rep.changed and rep.checked == 0
        assert [p.text for p in await db.sent_posts(1)] == ["first", "second", "third", "fifth"]  # My posts untouched

        chan.restriction = None  # only the posts carry the mark now (as real messages with restriction_reason)
        for i in (1, 2):
            chan.msgs[i].restriction_reason = [types.RestrictionReason("all", "copyright", notice_text)]
        rep = await sync_channel(client, db, ch, opts)
        assert sorted(rep.restricted) == [1, 2, 5] and rep.restricted_why in (notice_text, "Telegram shows a notice instead of the post")
        assert rep.deleted == [3] and rep.edited == []  # post 3 really is gone, the others were left alone
        assert [p.text for p in await db.sent_posts(1)] == ["first", "second", "fifth"]
        await db.close()

    asyncio.run(go())
    writes = (
        functions.messages.SendMessageRequest, functions.messages.SendMediaRequest, functions.messages.EditMessageRequest,
        functions.messages.ForwardMessagesRequest, functions.channels.DeleteMessagesRequest,
    )
    assert not [r for r in seen if isinstance(r, writes)]
    fulls = [r for r in seen if isinstance(r, functions.channels.GetFullChannelRequest)]
    assert len(fulls) == 3 and all(isinstance(r.channel, types.InputChannel) for r in fulls)
