"""Check that every Telegram call the bot makes builds a valid, serialisable MTProto request.

Telethon's network layer is replaced by a function that captures the request (after Telethon has
resolved it) and serialises it, so wrong argument types or shapes fail here instead of in production.
"""
import asyncio
import datetime

import pytest
from telethon import TelegramClient, functions, types, utils
from telethon.sessions import MemorySession

from app.common import push_edit, send_post
from app.tgutil import (
    build_markup,
    de_entities,
    edit_raw,
    media_from_ref,
    media_ref,
    peer_of,
    ser_entities,
)
from app.ui import button_keyboard, panel_keyboard

from .fakes import make_msg

NOW = datetime.datetime.now(datetime.timezone.utc)
CH = type("Ch", (), {"id": 1234, "access_hash": 5678})()


class Captured(Exception):
    def __init__(self, req):
        self.req = req


@pytest.fixture()
def client(monkeypatch):
    async def fake_call(self, request, ordered=False, flood_sleep_threshold=None):
        reqs = list(request) if isinstance(request, (list, tuple)) else [request]
        for r in reqs:
            if hasattr(r, "resolve"):
                await r.resolve(self, utils)
            assert isinstance(bytes(r), bytes)  # must serialise
        raise Captured(reqs[0])

    monkeypatch.setattr(TelegramClient, "__call__", fake_call)
    return TelegramClient(MemorySession(), 1, "h")


def capture(coro):
    async def go():
        try:
            await coro
        except Captured as c:
            return c.req
        raise AssertionError("no request was made")

    return asyncio.run(go())


def post(**kw):
    base = dict(
        id=1, message_id=50, text="Hello 😀", entities=[{"k": "bold", "o": 0, "l": 5}, {"k": "url", "o": 6, "l": 2, "u": "https://x.com"}],
        media_kind=None, media_file_id=None, link_preview=False,
        buttons=[[{"t": "Go", "u": "https://t.me/bot?start=a"}, {"t": "Two", "u": "https://t.me/bot?start=b"}]],
    )
    base.update(kw)
    return type("P", (), base)()


def photo_ref():
    photo = types.Photo(11, 22, b"ref", NOW, [types.PhotoSize("x", 100, 100, 999)], 2)
    return media_ref(make_msg(1, "", media=types.MessageMediaPhoto(photo=photo)))


def test_send_text_post(client):
    req = capture(send_post(client, peer_of(CH), post()))
    assert isinstance(req, functions.messages.SendMessageRequest)
    assert req.message == "Hello 😀" and req.no_webpage is True
    assert [type(e).__name__ for e in req.entities] == ["MessageEntityBold", "MessageEntityTextUrl"]
    assert req.reply_markup.rows[0].buttons[1].text == "Two"
    assert isinstance(req.peer, types.InputPeerChannel)


def test_send_media_post_with_and_without_file_reference(client):
    ref = photo_ref()
    req = capture(send_post(client, peer_of(CH), post(media_kind="photo", media_file_id=ref)))
    assert isinstance(req, functions.messages.SendMediaRequest)
    assert isinstance(req.media, types.InputMediaPhoto) and req.media.id.id == 11 and req.media.id.file_reference == b"ref"
    assert req.message == "Hello 😀" and req.reply_markup is not None

    async def blank():
        c = client
        _, media, _ = await c._file_to_media(media_from_ref(ref, blank_reference=True))
        return media

    assert asyncio.run(blank()).id.file_reference == b""


def test_raw_edits(client):
    p = post()
    req = capture(push_edit(client, CH, p))
    assert isinstance(req, functions.messages.EditMessageRequest)
    assert req.id == 50 and req.message == "Hello 😀" and req.no_webpage is True and req.reply_markup is not None
    # no buttons -> no reply_markup (Telegram then removes the keyboard)
    req = capture(push_edit(client, CH, post(buttons=[])))
    assert req.reply_markup is None and req.message == "Hello 😀"
    # markup-only edit leaves the text alone
    req = capture(edit_raw(client, peer_of(CH), 9, markup=build_markup([[{"t": "A", "u": "https://a.com"}]])))
    assert req.message is None and req.entities is None and req.reply_markup.rows[0].buttons[0].text == "A"


def test_media_replacement_edit(client):
    ref = photo_ref()
    req = capture(push_edit(client, CH, post(media_kind="photo", media_file_id=ref), replace_media=True))
    assert isinstance(req, functions.messages.EditMessageRequest)
    assert isinstance(req.media, types.InputMediaPhoto) and req.message == "Hello 😀"
    assert req.reply_markup is not None


def test_reads_deletes_and_rights_requests(client):
    peer = peer_of(CH)
    req = capture(client.get_messages(peer, ids=[1, 2, 3]))
    assert isinstance(req, functions.channels.GetMessagesRequest)
    assert isinstance(req.channel, types.InputChannel) and [i.id for i in req.id] == [1, 2, 3]
    req = capture(client.delete_messages(peer, [7]))
    assert isinstance(req, functions.channels.DeleteMessagesRequest) and req.id == [7]
    req = capture(client(functions.channels.GetFullChannelRequest(peer)))
    assert isinstance(req.channel, types.InputChannel)
    req = capture(client(functions.channels.GetParticipantRequest(channel=peer, participant=types.InputPeerSelf())))
    assert isinstance(req.participant, types.InputPeerSelf)


def test_entities_with_every_stored_kind_serialise():
    ents = [
        types.MessageEntityBold(0, 1), types.MessageEntityItalic(1, 1), types.MessageEntityUnderline(2, 1),
        types.MessageEntityStrike(3, 1), types.MessageEntitySpoiler(4, 1), types.MessageEntityCode(5, 1),
        types.MessageEntityPre(6, 1, "py"), types.MessageEntityTextUrl(7, 1, "https://x.com"),
        types.MessageEntityBlockquote(8, 1, True), types.MessageEntityCustomEmoji(9, 2, 123456789),
    ]
    back = de_entities(ser_entities(ents))
    assert [type(e) for e in back] == [type(e) for e in ents]
    assert back[8].collapsed is True
    for e in back:
        bytes(e)


def test_ui_keyboards_serialise(client):
    p = post()
    p.status, p.channel_id, p.source = "draft", 1, "bot"
    p.buttons = [[{"t": "A", "u": "https://a.com"}, {"t": "⚠️ no link", "u": None}], [{"t": "👍", "raw": ""}]]
    for kb in (panel_keyboard(p), button_keyboard(p, 0, 0), button_keyboard(p, 1, 0)):
        mk = client.build_reply_markup(kb)
        assert isinstance(bytes(mk), bytes)
    for label_row in panel_keyboard(p):
        for b in label_row:
            assert len(b.type.data) <= 64  # Telegram's limit for callback data


def test_button_callback_data_fits_for_large_ids():
    p = post(id=2_000_000_000)
    p.status, p.channel_id, p.source = "sent", -1, "bot"
    p.buttons = [[{"t": "x", "u": "https://a.com"}] * 8] * 12
    for row in panel_keyboard(p):
        for b in row:
            assert len(b.type.data) <= 64
