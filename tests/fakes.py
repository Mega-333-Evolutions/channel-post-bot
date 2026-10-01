"""Tiny in-memory stand-ins for Telethon, good enough to exercise the engine offline."""
import datetime
from types import SimpleNamespace

from telethon import errors, functions, types
from telethon.tl.custom import Message

from app.tgutil import make_url_button


def make_msg(i, text="", entities=None, markup=None, out=False, media=None):
    return Message(
        id=i,
        peer_id=types.PeerChannel(1),
        date=datetime.datetime.now(datetime.timezone.utc),
        message=text,
        out=out,
        entities=entities,
        reply_markup=markup,
        media=media,
    )


def url_btn(text, url):
    return make_url_button(text, url)


def cb_btn(text, data):
    return types.KeyboardButton(text=text, type=types.InlineButtonTypeCallback(data))


def markup(*rows):
    return types.ReplyInlineMarkup([types.KeyboardButtonRow(list(r)) for r in rows])


class FakeClient:
    def __init__(self, msgs, pts=None):
        self.msgs = dict(msgs)
        self.pts = pts
        self.edits = []
        self.fail = {}  # message id -> exception to raise on edit

    async def __call__(self, req):
        if isinstance(req, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(pts=self.pts))
        if isinstance(req, functions.messages.EditMessageRequest):
            if req.id in self.fail:
                raise self.fail[req.id]
            m = self.msgs[req.id]
            self.edits.append(req)
            if req.message is not None:
                m.message = req.message
                m.entities = list(req.entities or [])
                m.reply_markup = req.reply_markup  # like Telegram: no markup sent -> keyboard removed
            elif req.reply_markup is not None:
                m.reply_markup = req.reply_markup
            return None
        raise NotImplementedError(type(req))

    async def get_messages(self, peer, ids=None):
        return [self.msgs.get(i) for i in ids]


def rpc(name):
    return getattr(errors, name)(request=None)
