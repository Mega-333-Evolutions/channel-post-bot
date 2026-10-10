"""Tiny in-memory stand-ins for Telethon, good enough to exercise the engine offline."""
import datetime
from types import SimpleNamespace

from telethon import errors, functions, types
from telethon.tl.custom import Message

from app.tgutil import make_url_button


def make_msg(i, text="", entities=None, markup=None, out=False, media=None, grouped_id=None, reply_to=None, pinned=False):
    return Message(
        id=i,
        peer_id=types.PeerChannel(1),
        date=datetime.datetime.now(datetime.timezone.utc),
        message=text,
        out=out,
        entities=entities,
        reply_markup=markup,
        media=media,
        grouped_id=grouped_id,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=reply_to) if reply_to else None,
        pinned=True if pinned else None,
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


# ======================================================================================================
# A fake Telegram "channel": enough of the API for the repost / delete / buttons / userbot code, offline.
# ======================================================================================================
NOW = datetime.datetime.now(datetime.timezone.utc)
USER_HASH = 424242  # the channel's access hash as the userbot account sees it (differs from the bot's)


def photo_media(pid=11):
    photo = types.Photo(pid, pid * 7, b"ref%d" % pid, NOW, [types.PhotoSize("x", 100, 100, 999)], 2)
    return types.MessageMediaPhoto(photo=photo)


def video_media(did=21):
    doc = types.Document(did, did * 7, b"fr%d" % did, NOW, "video/mp4", 1000, 2, [types.DocumentAttributeVideo(5.0, 320, 240)])
    return types.MessageMediaDocument(document=doc)


def service_msg(i):
    return types.MessageService(id=i, peer_id=types.PeerChannel(1), date=NOW, action=types.MessageActionPinMessage())


def quote(offset, length, collapsed=None):
    return types.MessageEntityBlockquote(offset, length, collapsed)


def _bytes(obj):
    return bytes(obj) if obj is not None else None


def _empty_rights(r):
    return not any(v for k, v in r.to_dict().items() if k != "_")


class FakeChannelClient:
    """Telegram's side of ONE channel (peer id 1) as seen by the bot, with switches for the odd things Telegram does."""

    def __init__(self, msgs=None, *, username=None, cid=1, title="Test"):
        self.cid = cid  # this simulation is the channel with this id; add_channel() puts more channels next to it
        self.title = title
        self.registry = {cid: self}
        self.invite_hash = None  # a t.me/+hash invite link of this channel the bot "is in" (for CheckChatInvite)
        self.msgs = dict(msgs or {})
        self.username = username
        self.requests = []  # every request received, in order
        self.edits = []  # EditMessageRequests Telegram accepted
        self.delete_requests = []  # the id lists of channels.deleteMessages calls
        self._top = max(self.msgs, default=0)
        self._pts = self._top  # the channel's event counter: new messages, edits, deletes and pins all move it
        self._gid = 7000
        self._media = {}
        for m in self.msgs.values():
            self._remember_media(m)
        # --- switches
        self.restrict_forwards = False  # "Restrict saving content" is on
        self.hide_keyboard_on_create = 0  # the next N created posts show no keyboard until a real change (suspected bug)
        self.forward_hides_keyboard = False  # a forwarded copy keeps its keyboard "hidden": edits to the same keyboard are no-ops
        self.drop_all_markup = False  # Telegram never shows a keyboard (to test giving up)
        self.undeletable = set()  # ids the BOT may not delete (like posts older than 48 hours)
        self.delete_error = None  # raised by every delete request of the bot
        self.fail = {}  # request class name -> exception to raise
        self.rights = dict(admin=True, post=True, edit=True, delete=True, invite=False, add_admins=False)
        self.participants_visible = True
        self.known_users = {}  # user id -> types.User the bot has "met"
        self.members = set()  # user ids (other than the bot) that are in the channel
        self.admins = {}  # user id -> ChatAdminRights
        self.invites = {}  # hash -> dict(limit, uses, revoked)
        self.primary_invite = None  # the channel's own invite link
        self.require_approval = False
        self.users = {}  # user id -> types.User, for participant lists
        self.cant_delete_at_all = set()  # ids nobody can delete (userbot included)
        self.restriction = None  # text: Telegram restricts the whole channel (a copyright strike)
        self.about = ""  # the channel's description
        self.photo_id = None  # the channel's profile photo (its bytes are in _photo_data)
        self._photo_data = {}  # photo id -> bytes; shared by all channels next to each other
        self._uploads = {}  # uploaded file id -> bytes; shared as well
        self._counter = [9000]  # ids for photos and uploads; shared
        self.pinned_order = []  # message ids in the order they were pinned
        self._n_invites = 0

    # ----------------------------------------------------------------------------------- helpers
    def add_channel(self, other):
        """Put another channel next to this one: requests that name it (by channel id) are answered by it."""
        other.registry = self.registry
        self.registry[other.cid] = other
        self._media.update(other._media)
        other._media = self._media  # a photo or file is known to the account, whichever channel it is sent to
        self._photo_data.update(other._photo_data)
        other._photo_data, other._uploads, other._counter = self._photo_data, self._uploads, self._counter
        return other

    def set_photo(self, data: bytes) -> int:
        """Give the channel a profile photo (what it looks like is the bytes)."""
        self._counter[0] += 1
        self.photo_id = self._counter[0]
        self._photo_data[self.photo_id] = data
        return self.photo_id

    def photo_bytes(self):
        return self._photo_data.get(self.photo_id) if self.photo_id else None

    def _chat_photo(self):
        if not self.photo_id:
            return types.PhotoEmpty(0)
        return types.Photo(self.photo_id, 1, b"ref", NOW, [types.PhotoSize("x", 100, 100, 999)], 2)

    async def download_media(self, media, file=None):
        if isinstance(media, types.Photo) and media.id in self._photo_data:
            return self._photo_data[media.id]
        raise ValueError("this fake can only download a channel's profile photo")

    async def upload_file(self, data, file_name=None):
        self._counter[0] += 1
        self._uploads[self._counter[0]] = bytes(data)
        return types.InputFile(id=self._counter[0], parts=1, name=file_name or "file", md5_checksum="")

    def _target(self, req):
        """The channel a request is about (a forward is carried out by its destination)."""
        peer = getattr(req, "to_peer", None) or getattr(req, "peer", None) or getattr(req, "channel", None)
        cid = getattr(peer, "channel_id", None)
        return self.registry.get(cid, self) if cid is not None else self

    def _remember_media(self, m):
        md = getattr(m, "media", None)
        if isinstance(md, types.MessageMediaPhoto) and md.photo:
            self._media[("photo", md.photo.id)] = md
        elif isinstance(md, types.MessageMediaDocument) and md.document:
            self._media[("doc", md.document.id)] = md

    def _media_for(self, inp):
        if isinstance(inp, types.InputMediaPhoto):
            key = ("photo", inp.id.id)
        elif isinstance(inp, types.InputMediaDocument):
            key = ("doc", inp.id.id)
        else:
            raise rpc("MediaInvalidError")
        if key not in self._media:
            raise rpc("MediaEmptyError")
        return self._media[key]

    def _effective(self, m):
        hidden = getattr(m, "_hidden", None)
        return hidden if hidden is not None else m.reply_markup

    def _set_markup(self, m, markup, *, hide=False):
        if markup is not None and (hide or self.drop_all_markup):
            m._hidden, m.reply_markup = markup, None
        else:
            m._hidden, m.reply_markup = None, markup

    def _new(self, text, entities, markup, media=None, gid=None, invert=None, hide=False, reply_to=None):
        self._top += 1
        self._pts += 1
        m = Message(
            id=self._top,
            peer_id=types.PeerChannel(self.cid),
            date=NOW,
            message=text,
            out=True,
            entities=list(entities) if entities else None,
            media=media,
            grouped_id=gid,
            invert_media=invert,
            reply_to=types.MessageReplyHeader(reply_to_msg_id=reply_to) if reply_to else None,
        )
        if self.hide_keyboard_on_create > 0 and markup is not None:
            hide = True
            self.hide_keyboard_on_create -= 1
        self._set_markup(m, markup, hide=hide)
        self.msgs[m.id] = m
        self._remember_media(m)
        return m

    def _updates(self, msgs):
        return types.Updates(
            updates=[types.UpdateNewChannelMessage(message=m, pts=m.id, pts_count=1) for m in msgs],
            users=[],
            chats=[],
            date=NOW,
            seq=0,
        )

    def _channel_obj(self):
        extra = {}
        if self.restriction:
            extra = dict(
                restricted=True,
                restriction_reason=[types.RestrictionReason(platform="all", reason="copyright", text=self.restriction)],
            )
        return types.Channel(
            id=self.cid, title=self.title, photo=types.ChatPhotoEmpty(), date=NOW, access_hash=USER_HASH,
            username=self.username, broadcast=True, **extra,
        )

    # --------------------------------------------------------------------------------- dispatcher
    async def __call__(self, req):
        target = self._target(req)
        if target is not self:
            return await target(req)
        bytes(req)  # every request the bot builds must serialise
        self.requests.append(req)
        name = type(req).__name__
        err = self.fail.get(name)
        if err is not None:
            raise err
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            raise NotImplementedError(name)
        return handler(req)

    async def get_messages(self, peer, ids=None):
        chan = self.registry.get(getattr(peer, "channel_id", None), self)
        err = chan.fail.get("GetMessagesRequest")  # reading a channel the bot may not read (a real client sends this request)
        if err is not None:
            raise err
        return [chan.msgs.get(i) for i in ids]

    async def get_input_entity(self, peer):
        if isinstance(peer, int) and peer in self.known_users:
            u = self.known_users[peer]
            return types.InputPeerUser(u.id, u.access_hash)
        raise ValueError(f"Could not find the input entity for {peer!r}")

    # ------------------------------------------------------------------------- message requests
    def _do_GetMessagesRequest(self, req):
        """channels.getMessages as Telegram answers it: the posts as they are now, MessageEmpty for missing ones.
        (Used when a real Telethon client sits in front of this simulation; get_messages() above serves the plain fakes.)"""
        out = []
        for ref in req.id:
            m = self.msgs.get(ref.id)
            if m is None:
                out.append(types.MessageEmpty(id=ref.id, peer_id=types.PeerChannel(self.cid)))
            elif isinstance(m, (types.MessageService, types.MessageEmpty)):
                out.append(m)
            else:
                out.append(
                    types.Message(
                        id=m.id, peer_id=m.peer_id, date=m.date, message=m.message, out=m.out, entities=m.entities,
                        reply_markup=m.reply_markup, media=m.media, grouped_id=m.grouped_id,
                        invert_media=getattr(m, "invert_media", None), reply_to=m.reply_to, pinned=m.pinned,
                        restriction_reason=getattr(m, "restriction_reason", None),
                    )
                )
        return types.messages.ChannelMessages(
            pts=self._top, count=len(out), messages=out, topics=[], chats=[self._channel_obj()], users=[]
        )

    def _do_ForwardMessagesRequest(self, req):
        origin = self.registry.get(getattr(req.from_peer, "channel_id", None), self)
        if origin.restrict_forwards:  # "Restrict saving content" is a setting of the channel the posts come from
            raise rpc("ChatForwardsRestrictedError")
        out, gids = [], {}
        for i in req.id:
            src = origin.msgs.get(i)
            if src is None:
                continue
            gid = gids.setdefault(src.grouped_id, self._next_gid()) if src.grouped_id else None
            out.append(
                self._new(
                    src.message, src.entities, src.reply_markup, src.media, gid, getattr(src, "invert_media", None),
                    hide=self.forward_hides_keyboard,
                )
            )
        if not out:
            raise rpc("MessageIdInvalidError")
        return self._updates(out)

    def _next_gid(self):
        self._gid += 1
        return self._gid

    @staticmethod
    def _reply_id(req):
        r = getattr(req, "reply_to", None)
        return r.reply_to_msg_id if r is not None else None

    def _check_reply(self, req):
        rid = self._reply_id(req)
        if rid is not None and rid not in self.msgs:
            raise rpc("MessageIdInvalidError")  # REPLY_MESSAGE_ID_INVALID
        return rid

    def _do_SendMessageRequest(self, req):
        rid = self._check_reply(req)
        return self._updates([self._new(req.message, req.entities, req.reply_markup, None, None, req.invert_media, reply_to=rid)])

    def _do_SendMediaRequest(self, req):
        media = self._media_for(req.media)
        rid = self._check_reply(req)
        return self._updates([self._new(req.message, req.entities, req.reply_markup, media, None, req.invert_media, reply_to=rid)])

    def _do_SendMultiMediaRequest(self, req):
        gid = self._next_gid()
        rid = self._check_reply(req)
        out = [
            self._new(s.message, s.entities, None, self._media_for(s.media), gid, req.invert_media, reply_to=rid)
            for s in req.multi_media
        ]
        return self._updates(out)

    def _do_UpdatePinnedMessageRequest(self, req):
        """Pinning in a channel needs the "Edit messages of others" right; Telegram adds a "pinned" notice to the channel."""
        if not self.rights.get("edit"):
            raise rpc("ChatAdminRequiredError")
        m = self.msgs.get(req.id)
        if m is None or isinstance(m, types.MessageService):
            raise rpc("MessageIdInvalidError")
        if req.unpin:
            m.pinned = None
            return self._updates([])
        if m.pinned:
            raise rpc("MessageNotModifiedError")
        m.pinned = True
        self.pinned_order.append(req.id)
        self._top += 1
        self._pts += 2
        note = types.MessageService(
            id=self._top, peer_id=types.PeerChannel(self.cid), date=NOW, action=types.MessageActionPinMessage(),
            reply_to=types.MessageReplyHeader(reply_to_msg_id=req.id),
        )
        self.msgs[note.id] = note
        return self._updates([note])

    def _do_EditMessageRequest(self, req):
        m = self.msgs.get(req.id)
        if m is None:
            raise rpc("MessageIdInvalidError")
        if not getattr(m, "out", False) and not self.rights.get("edit"):  # somebody else's post needs that right
            raise rpc("MessageAuthorRequiredError")
        if getattr(m, "via_other_bot", False):  # buttons or a post made through another bot: only that bot may edit
            raise rpc("MessageIdInvalidError")
        if req.message is None:  # markup-only edit
            text, ents = m.message, list(m.entities or [])
            markup = req.reply_markup if req.reply_markup is not None else self._effective(m)
        else:
            text, ents, markup = req.message, list(req.entities or []), req.reply_markup  # None = keyboard removed
        if (
            text == m.message
            and [_bytes(e) for e in ents] == [_bytes(e) for e in (m.entities or [])]
            and _bytes(markup) == _bytes(self._effective(m))
        ):
            raise rpc("MessageNotModifiedError")
        m.message, m.entities = text, (ents or None)
        self._set_markup(m, markup)
        self.edits.append(req)
        self._pts += 1
        return self._updates([])

    def _do_DeleteMessagesRequest(self, req):
        ids = list(req.id)
        self.delete_requests.append(ids)
        if self.delete_error is not None:
            raise self.delete_error
        n = 0
        for i in ids:
            if i in self.undeletable or i in self.cant_delete_at_all or i not in self.msgs:
                continue
            del self.msgs[i]
            n += 1
        self._pts += n
        return types.messages.AffectedMessages(pts=self._pts, pts_count=n)

    # ------------------------------------------------------------------------ channel / rights
    def _do_GetFullChannelRequest(self, req):
        inv = types.ChatInviteExported(link=self.primary_invite, admin_id=1, date=NOW) if self.primary_invite else None
        return SimpleNamespace(
            full_chat=SimpleNamespace(
                pts=max(self._pts, self._top), exported_invite=inv, about=self.about, chat_photo=self._chat_photo()
            ),
            chats=[self._channel_obj()],
        )

    def _notice(self, action):
        """The service message Telegram puts into a channel when its name or photo changes."""
        self._top += 1
        self._pts += 1
        note = types.MessageService(id=self._top, peer_id=types.PeerChannel(self.cid), date=NOW, action=action)
        self.msgs[note.id] = note
        return note

    def _need_info_right(self):
        if not self.rights.get("change_info", True):
            raise rpc("ChatAdminRequiredError")

    def _do_EditTitleRequest(self, req):
        self._need_info_right()
        if req.title == self.title:
            raise rpc("ChatNotModifiedError")
        self.title = req.title
        return self._updates([self._notice(types.MessageActionChatEditTitle(req.title))])

    def _do_EditChatAboutRequest(self, req):
        self._need_info_right()
        if req.about == self.about:
            raise rpc("ChatAboutNotModifiedError")
        self.about = req.about
        return True

    def _do_EditPhotoRequest(self, req):
        self._need_info_right()
        data = self._uploads[req.photo.file.id]
        self.set_photo(data)
        return self._updates([self._notice(types.MessageActionChatEditPhoto(self._chat_photo()))])

    def _do_GetParticipantRequest(self, req):
        r = self.rights
        if r is None:
            raise rpc("UserNotParticipantError")
        if not r.get("admin", True):
            return SimpleNamespace(participant=types.ChannelParticipant(user_id=1, date=NOW))
        rights = types.ChatAdminRights(
            post_messages=r.get("post"), edit_messages=r.get("edit"), delete_messages=r.get("delete"),
            invite_users=r.get("invite"), add_admins=r.get("add_admins"), change_info=r.get("change_info", True),
        )
        return SimpleNamespace(participant=types.ChannelParticipantAdmin(user_id=1, promoted_by=1, date=NOW, admin_rights=rights))

    def _do_ExportChatInviteRequest(self, req):
        if not self.rights.get("invite"):
            raise rpc("ChatAdminRequiredError")
        self._n_invites += 1
        h = f"AbCdEfGh{self._n_invites:04d}"
        self.invites[h] = dict(limit=req.usage_limit, uses=0, revoked=False, title=req.title, expire=req.expire_date)
        return types.ChatInviteExported(
            link=f"https://t.me/+{h}", admin_id=1, date=NOW, usage_limit=req.usage_limit, expire_date=req.expire_date, title=req.title
        )

    def _do_EditExportedChatInviteRequest(self, req):
        h = req.link.rsplit("+", 1)[-1]
        if h in self.invites and req.revoked:
            self.invites[h]["revoked"] = True
        return SimpleNamespace(invite=None)

    def _do_EditAdminRequest(self, req):
        if not self.rights.get("add_admins"):
            raise rpc("ChatAdminRequiredError")
        uid = req.user_id.user_id
        known = self.users.get(uid)
        if known is None or req.user_id.access_hash != known.access_hash:
            raise rpc("UserIdInvalidError")
        if _empty_rights(req.admin_rights):
            self.admins.pop(uid, None)
        else:
            self.admins[uid] = req.admin_rights
        return self._updates([])

    def _do_GetParticipantsRequest(self, req):
        if not self.participants_visible:
            raise rpc("ChatAdminRequiredError")
        users = [self.users[u] for u in sorted(self.members) if u in self.users]
        if isinstance(req.filter, types.ChannelParticipantsSearch):
            q = req.filter.q.lower()
            users = [u for u in users if q in (u.username or "").lower() or q in (u.first_name or "").lower()]
        return types.channels.ChannelParticipants(count=len(users), participants=[], chats=[], users=users)


class FakeUserClient:
    """The userbot account's own Telegram connection, sharing one FakeChannelClient with the bot."""

    def __init__(self, chan, *, user_id=777, authorized=True, bot=False, username="helper_acct", first_name="Helper"):
        self.chan = chan
        self.me = types.User(id=user_id, access_hash=555, first_name=first_name, username=username, bot=bot or None)
        chan.users[user_id] = self.me
        self.authorized = authorized
        self.connected = False
        self.known_channel = False
        self.requests = []
        self.deleted = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.me

    async def get_input_entity(self, peer):
        if isinstance(peer, types.PeerChannel) and self.known_channel:
            return types.InputPeerChannel(peer.channel_id, USER_HASH)
        if isinstance(peer, str) and self.chan.username and peer.lstrip("@").lower() == self.chan.username.lower():
            return types.InputPeerChannel(1, USER_HASH)
        raise ValueError(f"Could not find the input entity for {peer!r}")

    async def iter_dialogs(self):
        if self.known_channel and self.me.id in self.chan.members:
            yield SimpleNamespace(entity=self.chan._channel_obj())

    def _join(self):
        self.chan.members.add(self.me.id)
        self.known_channel = True

    async def __call__(self, req):
        bytes(req)
        self.requests.append(req)
        name = type(req).__name__
        chan = self.chan
        err = chan.fail.get("user:" + name)
        if err is not None:
            raise err
        uid = self.me.id
        if name == "ImportChatInviteRequest":
            inv = chan.invites.get(req.hash)
            if inv is None:
                raise rpc("InviteHashInvalidError")
            if inv["revoked"] or (inv["limit"] and inv["uses"] >= inv["limit"]):
                raise rpc("InviteHashExpiredError")
            if uid in chan.members:
                raise rpc("UserAlreadyParticipantError")
            if chan.require_approval:
                raise rpc("InviteRequestSentError")
            inv["uses"] += 1
            self._join()
            return types.Updates(updates=[], users=[], chats=[chan._channel_obj()], date=NOW, seq=0)
        if name == "CheckChatInviteRequest":
            if uid in chan.members:
                return types.ChatInviteAlready(chat=chan._channel_obj())
            return types.ChatInvite(title="Test", photo=types.PhotoEmpty(0), participants_count=1, color=0)
        if name == "JoinChannelRequest":
            if uid in chan.members:
                raise rpc("UserAlreadyParticipantError")
            self._join()
            return types.Updates(updates=[], users=[], chats=[chan._channel_obj()], date=NOW, seq=0)
        if name == "GetParticipantRequest":
            if uid not in chan.members:
                raise rpc("UserNotParticipantError")
            if uid in chan.admins:
                return SimpleNamespace(
                    participant=types.ChannelParticipantAdmin(user_id=uid, promoted_by=1, date=NOW, admin_rights=chan.admins[uid])
                )
            return SimpleNamespace(participant=types.ChannelParticipant(user_id=uid, date=NOW))
        if name == "DeleteMessagesRequest":
            ids = list(req.id)
            self.deleted.append(ids)
            adm = chan.admins.get(uid)
            if adm is None or not adm.delete_messages:
                raise rpc("ChatAdminRequiredError")
            n = 0
            for i in ids:
                if i in chan.msgs and i not in chan.cant_delete_at_all:
                    del chan.msgs[i]
                    n += 1
            return types.messages.AffectedMessages(pts=chan._top, pts_count=n)
        raise NotImplementedError(name)

