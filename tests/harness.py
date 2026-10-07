"""Drive the real handlers with fake events and a fake Telegram client (no network)."""
import itertools
import re
from types import SimpleNamespace

from telethon import events, functions, types

from app.common import Ctx
from app.config import Config
from app.db import Database

from bot import register_all

from .dbutil import db_url_for
from .fakes import FakeChannelClient


class FakeSent:
    """A message the bot sent to the user (so `.edit()` works on it)."""

    def __init__(self, out, text, buttons):
        self.out, self.text, self.buttons = out, text, buttons

    async def edit(self, text=None, buttons=None, **kw):
        if text is not None:
            self.text = text
        self.buttons = buttons
        self.out.log.append(("edit", self.text, buttons))


class FakeEvent:
    def __init__(self, uid, out, text="", data=None, message=None):
        self.sender_id, self.out = uid, out
        self.is_private = True
        self.is_channel = False
        self.is_group = False
        self.raw_text = text
        self.text = text
        self.message = message or SimpleNamespace(
            raw_text=text, entities=[], media=None, grouped_id=None, forward=None
        )
        self.pattern_match = None
        self._is_callback = data is not None
        if data is not None:
            self.data = data.encode()

    async def respond(self, text=None, buttons=None, file=None, **kw):
        self.out.log.append(("say", text, buttons))
        return FakeSent(self.out, text, buttons)

    async def edit(self, text=None, buttons=None, **kw):
        self.out.log.append(("edit", text, buttons))

    async def answer(self, *a, **kw):
        return None

    async def get_input_chat(self):
        return types.InputPeerUser(self.sender_id, 0)


class Outbox:
    def __init__(self):
        self.log = []

    @property
    def last_text(self):
        return self.log[-1][1] if self.log else None

    def last_buttons(self):
        b = self.log[-1][2]
        return [[getattr(x, "text", None) for x in row] for row in (b or [])]

    def callback_data(self, label_part):
        """Data of the first inline button (in the last message) whose label contains `label_part`."""
        for row in self.log[-1][2] or []:
            for btn in row:
                if label_part in btn.text:
                    t = btn.type if hasattr(btn, "type") else btn
                    return t.data.decode()
        raise AssertionError(f"no button {label_part!r} in {self.last_buttons()}")


class FakeTG(FakeChannelClient):
    """The bot's Telegram connection: a simulated channel plus a log of what the bot sent to the user."""

    def __init__(self):
        super().__init__()
        self.handlers = []
        self.sent = []
        self.edits = []
        self.deleted = []
        self.ids = itertools.count(100)
        self.parse_mode = "html"
        self.rights = dict(admin=True, post=True, edit=True, delete=True, invite=False, add_admins=False)

    def on(self, builder):
        def deco(fn):
            self.handlers.append((builder, fn))
            return fn

        return deco

    async def send_message(self, peer, message="", **kw):
        m = SimpleNamespace(id=next(self.ids))
        self.sent.append(("text", peer, message, kw))
        return m

    async def send_file(self, peer, file, **kw):
        m = SimpleNamespace(id=next(self.ids))
        self.sent.append(("file", peer, file, kw))
        return m

    async def edit_message(self, peer, message, **kw):
        self.edits.append(("edit_message", message, kw))

    async def delete_messages(self, peer, ids, **kw):
        self.deleted.append(list(ids))
        return [SimpleNamespace(pts_count=len(ids))]

    async def get_entity(self, what):
        """Public channels by @username, or any channel by PeerChannel - the ones put next to this one."""
        for chan in self.registry.values():
            if isinstance(what, str) and chan.username and chan.username.lower() == what.lstrip("@").lower():
                return chan._channel_obj()
            if isinstance(what, types.PeerChannel) and what.channel_id == chan.cid:
                return chan._channel_obj()
        raise ValueError(f"Cannot find any entity corresponding to {what!r}")

    async def __call__(self, req):
        if isinstance(req, functions.messages.CheckChatInviteRequest):
            for chan in self.registry.values():
                if chan.invite_hash == req.hash:
                    return types.ChatInviteAlready(chat=chan._channel_obj())
            raise ValueError("INVITE_HASH_INVALID")
        target = self._target(req)
        if target is not self:
            return await target(req)
        if isinstance(req, functions.messages.EditMessageRequest):
            self.edits.append(("raw", req.id, req))
            err = self.fail.get("EditMessageRequest")
            if err is not None:
                raise err
            if req.id not in self.msgs:  # a post the simulation doesn't hold: just note the edit
                return None
        if isinstance(req, functions.channels.DeleteMessagesRequest):
            self.deleted.append(list(req.id))
        return await super().__call__(req)


class App:
    def __init__(self, tmp_path, owner=1, **cfg_overrides):
        base = dict(api_id=1, api_hash="x", bot_token="1:x", owners=frozenset({owner}), admins=frozenset(),
                    database_url=db_url_for(tmp_path, "h.db"), db_pool="null", session_path="x", session_string="",
                    edit_delay=0.3, replace_typed_links=True, port=0, log_level="INFO")
        base.update(cfg_overrides)
        cfg = Config(**base)
        self.tg = FakeTG()
        self.db = Database(cfg.database_url)
        self.ctx = Ctx(cfg=cfg, db=self.db, client=self.tg)
        self.uid = owner
        self.out = Outbox()

    async def start(self):
        from app.db import Base

        async with self.db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.db.init()
        register_all(self.ctx)

    async def text(self, s, message=None):
        ev = FakeEvent(self.uid, self.out, s, message=message)
        for builder, cb in self.tg.handlers:
            if not isinstance(builder, events.NewMessage):
                continue
            if builder.pattern:
                m = builder.pattern(s)
                if not m:
                    continue
                ev.pattern_match = m
            if builder.func and not builder.func(ev):
                continue
            await cb(ev)
        return ev

    async def raw(self, update):
        """Hand a raw Telegram update (a new / edited / deleted channel message ...) to the handlers that listen for it."""
        for builder, cb in self.tg.handlers:
            if isinstance(builder, events.Raw) and builder.filter(update) is not None:
                await cb(update)

    async def press(self, data):
        ev = FakeEvent(self.uid, self.out, data=data)
        for builder, cb in self.tg.handlers:
            if isinstance(builder, events.CallbackQuery):
                await cb(ev)
        return ev


def norm(s):
    import html

    return html.unescape(re.sub(r"<[^>]+>", "", s or ""))
