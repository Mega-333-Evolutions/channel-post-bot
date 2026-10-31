"""A userbot - a normal Telegram account - that does what the bot is not allowed to do in a channel.

Deleting: Telegram's documentation says bots can only delete messages younger than 48 hours. The bot always tries
first; whatever is left goes to the userbot.
Editing: Telegram refuses the bot an edit of a post that belongs to another bot (its buttons were added by that bot,
or it was posted through it). The bot always tries first; when Telegram refuses, the userbot makes the edit (text,
caption or a new photo / file). A user account can't set inline buttons, so Telegram may drop them with that edit -
the bot puts them back where it can.
A new photo / file goes to the userbot through the userbot's own chat with the bot: the bot sends it there (Telegram
just points to the file it already has - nothing is uploaded), the userbot finds it in that chat and edits the post
with that copy, again by reference. So a big file is as quick as a small one and keeps its thumbnail. Only when that
chat does not work (the userbot account never started the bot) is the file downloaded and uploaded again.

How it gets into the channel:
  1. the bot makes a one-time invite link (or reads the channel's own link) and the userbot joins with it
     (public channels without a usable link: the userbot joins by name);
  2. if the userbot is not allowed to do the job there yet and the bot has the "Add new admins" right, the bot
     makes the userbot an admin with that one right ("Delete messages" / "Edit messages of others");
  3. the userbot does the job, and afterwards the bot gives the admin rights back as they were
     (USERBOT_KEEP_ADMIN=true keeps the new right).
The session lives in USERBOT_SESSION (made on your own computer with make_userbot_session.py).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
from typing import Optional

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

from .tgutil import MediaFile, edit_raw, flood_retry, get_rights, media_from_ref, media_key, peer_of, with_media

log = logging.getLogger(__name__)

_INVITE_RX = re.compile(
    r"(?i)(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([A-Za-z0-9_-]{8,})"
    r"|tg://join\?invite=([A-Za-z0-9_-]{8,})"
)


class UserbotError(Exception):
    """Something the owner can fix; the text is meant to be shown."""


class UserbotRefused(UserbotError):
    """Telegram refused what the userbot tried; `name` is the Telegram error (e.g. MediaInvalidError)."""

    def __init__(self, text: str, name: str = ""):
        super().__init__(text)
        self.name = name

    @property
    def about_media(self) -> bool:
        """The refusal is about the photo / file the userbot used (so trying it another way may help)."""
        return self.name in _MEDIA_REFUSED


# What Telegram answers when the photo / file the userbot points to can't be used for an edit
_MEDIA_REFUSED = {
    "MediaInvalidError", "MediaEmptyError", "DocumentInvalidError", "PhotoInvalidError",
    "FileReferenceExpiredError", "FileReferenceInvalidError", "FileReferenceEmptyError",
}
# What Telegram answers when the bot may not write to the userbot account (it never started the bot, or blocked it)
_NOT_STARTED = {
    "UserIsBlockedError", "PeerIdInvalidError", "InputUserDeactivatedError", "UserDeactivatedError", "UserIdInvalidError",
}
_DM_POLLS = 10  # how often, and how long apart, the userbot looks in its chat with the bot for the file it was sent
_DM_WAIT = 0.5


def parse_invite_hash(link: str) -> Optional[str]:
    m = _INVITE_RX.search(link or "")
    return (m.group(1) or m.group(2)) if m else None


def _default_factory(cfg) -> TelegramClient:
    return TelegramClient(StringSession(cfg.userbot_session), cfg.api_id, cfg.api_hash, receive_updates=False)


class Userbot:
    def __init__(self, bot_client, cfg, client_factory=None):
        self.bot = bot_client
        self.cfg = cfg
        self._factory = client_factory or _default_factory
        self.client = None
        self.me = None
        self.last_error: Optional[str] = None
        self.lock = asyncio.Lock()  # one channel job at a time (join / promote / delete or edit / demote)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "userbot_session", ""))

    @property
    def ready(self) -> bool:
        return self.client is not None

    @property
    def keep_admin(self) -> bool:
        return bool(getattr(self.cfg, "userbot_keep_admin", False))

    async def connect(self) -> bool:
        """Log the userbot in. True when it is usable; otherwise the reason is in `last_error`."""
        if not self.enabled:
            self.last_error = "USERBOT_SESSION is not set."
            return False
        if self.client is not None:
            return True
        client = None
        try:
            client = self._factory(self.cfg)
            await client.connect()
            if not await client.is_user_authorized():
                raise UserbotError(
                    "The userbot session is not logged in any more (or it was revoked). "
                    "Make a new one with make_userbot_session.py."
                )
            me = await client.get_me()
            if getattr(me, "bot", False):
                raise UserbotError("USERBOT_SESSION belongs to a bot. The userbot has to be a normal user account.")
        except Exception as e:
            self.last_error = str(e) if isinstance(e, UserbotError) else f"{type(e).__name__}: {str(e)[:200]}"
            log.warning("the userbot is not available: %s", self.last_error)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            return False
        self.client, self.me, self.last_error = client, me, None
        log.info("userbot logged in as %s (id %s)", getattr(me, "username", None) or getattr(me, "first_name", ""), me.id)
        return True

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    def describe(self) -> str:
        if not self.enabled:
            return "not set up (USERBOT_SESSION is empty)"
        if self.ready:
            name = getattr(self.me, "username", None)
            who = f"@{name}" if name else (getattr(self.me, "first_name", "") or "the account")
            return f"connected as {who} (id {self.me.id})"
        return f"set up, but not connected: {self.last_error or 'unknown reason'}"

    def deleter(self, ch) -> "UserbotDeleter":
        return UserbotDeleter(self, ch)

    def editor(self, ch) -> "UserbotEditor":
        return UserbotEditor(self, ch)

    async def fallback_for(self, ch):
        """(delete function, deleter) to hand to the delete step, or (None, None) when no userbot is usable.

        Nothing happens in the channel until the bot has failed to delete something and the function is called;
        the caller must always `await deleter.close()` afterwards.
        """
        if not self.enabled:
            return None, None
        if not self.ready:
            await self.connect()
        if not self.ready:
            return None, None
        d = self.deleter(ch)
        return d.delete, d

    async def editor_for(self, ch) -> Optional["UserbotEditor"]:
        """An editor for posts in `ch`, or None when no userbot is usable. The caller must always `await editor.close()`."""
        if not self.enabled:
            return None
        if not self.ready:
            await self.connect()
        return self.editor(ch) if self.ready else None


def _rights_dict(rights) -> dict:
    return {k: v for k, v in rights.to_dict().items() if k != "_"} if rights is not None else {}


class _ChannelJob:
    """Everything needed to do ONE job in ONE channel with the userbot. Call close() when done, whatever happened.

    What the job needs is named by RIGHT (the field of ChatAdminRights) and RIGHT_NAME (what the Telegram apps call it).
    """

    RIGHT = "delete_messages"
    RIGHT_NAME = "Delete messages"
    WHAT = "delete messages"  # what the userbot may not do without the right (as the owner is told)
    VERB = "delete"

    def __init__(self, ub: Userbot, ch):
        self.ub = ub
        self.ch = ch
        self.peer = None  # the channel as the userbot sees it
        self.notes: list = []
        self._ready = False
        self._holds_lock = False
        self._promoted = False
        self._before = None  # (admin rights, rank) the userbot had before the bot changed them; None = a plain member
        self._bot_user = None  # the userbot account as the bot sees it (InputUser)
        self._link: Optional[str] = None  # the temporary invite link we made

    # ------------------------------------------------------------------ public
    async def _begin(self) -> None:
        """Get the channel ready (joined, with the right). The userbot's lock is kept until close()."""
        if not self.ub.ready:
            raise UserbotError(f"The userbot is not connected: {self.ub.last_error or 'unknown reason'}")
        if not self._ready:
            if not self._holds_lock:  # released again in close()
                await self.ub.lock.acquire()
                self._holds_lock = True
            await self._prepare()
            self._ready = True

    async def close(self) -> None:
        """Undo what this job changed: give the admin rights back as they were, revoke the temporary link."""
        bot, ch = self.ub.bot, self.ch
        try:
            if self._promoted and self._bot_user is not None and not self.ub.keep_admin:
                rights, rank = self._before or (types.ChatAdminRights(), "")
                try:
                    await bot(
                        functions.channels.EditAdminRequest(
                            channel=peer_of(ch), user_id=self._bot_user, admin_rights=rights, rank=rank
                        )
                    )
                    self.notes.append(
                        "took the admin right away from the userbot again"
                        if self._before is None
                        else "put the userbot's admin rights back as they were"
                    )
                except Exception as e:
                    log.warning("could not demote the userbot in %s: %s", ch.id, type(e).__name__)
                    self.notes.append("could not take the admin right away from the userbot - please do it by hand")
            if self._link:
                try:
                    await bot(
                        functions.messages.EditExportedChatInviteRequest(peer=peer_of(ch), link=self._link, revoked=True)
                    )
                except Exception as e:
                    log.info("could not revoke the temporary invite link: %s", type(e).__name__)
        finally:
            self._promoted, self._link = False, None
            if self._holds_lock:
                self._holds_lock = False
                self.ub.lock.release()

    # ----------------------------------------------------------------- prepare
    async def _prepare(self) -> None:
        peer = await self._member_peer()
        if peer is None:
            peer = await self._join()
        self.peer = peer
        await self._ensure_right()

    async def _member_peer(self):
        """The channel as the userbot sees it, if the userbot is in it already."""
        ub = self.ub.client
        try:
            peer = await ub.get_input_entity(types.PeerChannel(self.ch.id))
            if not isinstance(peer, types.InputPeerChannel):
                return None
            await ub(functions.channels.GetParticipantRequest(peer, types.InputPeerSelf()))
        except (errors.RPCError, ValueError, TypeError):
            return None
        return peer

    async def _invite_link(self) -> Optional[str]:
        bot, peer = self.ub.bot, peer_of(self.ch)
        try:
            expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            res = await bot(
                functions.messages.ExportChatInviteRequest(peer=peer, expire_date=expires, usage_limit=1, title="Cleanup helper")
            )
            link = getattr(res, "link", None)
            if link:
                self._link = link
                self.notes.append("made a one-time invite link for the userbot")
                return link
        except errors.RPCError as e:
            log.info("could not create an invite link in %s: %s", self.ch.id, type(e).__name__)
        try:
            full = await bot(functions.channels.GetFullChannelRequest(peer))
            link = getattr(getattr(full.full_chat, "exported_invite", None), "link", None)
            if link:
                self.notes.append("used the channel's own invite link")
                return link
        except errors.RPCError as e:
            log.info("could not read the invite link of %s: %s", self.ch.id, type(e).__name__)
        return None

    async def _join(self):
        link = await self._invite_link()
        h = parse_invite_hash(link) if link else None
        if h:
            peer = await self._import_invite(h)
            if peer is not None:
                self.notes.append("the userbot joined the channel")
                return peer
        if self.ch.username:
            peer = await self._join_public(self.ch.username)
            self.notes.append("the userbot joined the channel by its public name")
            return peer
        raise UserbotError(
            "I could not get the userbot into this private channel: Telegram gave me no invite link. "
            "Give the bot the “Invite users via link” admin right, or add the userbot to the channel yourself "
            f"(with the “{self.RIGHT_NAME}” admin right)."
        )

    async def _import_invite(self, h: str):
        ub = self.ub.client
        try:
            res = await flood_retry(lambda: ub(functions.messages.ImportChatInviteRequest(h)))
        except errors.UserAlreadyParticipantError:
            return await self._peer_when_already_in(h)
        except errors.InviteRequestSentError:
            raise UserbotError(
                "This channel's invite link needs an admin to approve new members. Approve the userbot's join request "
                "(or add the userbot yourself) and try again."
            )
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError, errors.InviteHashEmptyError):
            return None
        except (errors.ChannelsTooMuchError, errors.UserChannelsTooMuchError):
            raise UserbotError("The userbot account is in too many channels. Leave a few with that account and try again.")
        except errors.UserBannedInChannelError:
            raise UserbotError("The userbot account is banned in this channel. Unban it first.")
        return self._peer_from_updates(res)

    def _peer_from_updates(self, res):
        chats = [c for c in (getattr(res, "chats", None) or []) if isinstance(c, types.Channel)]
        for c in chats:
            if c.id == self.ch.id:
                return utils.get_input_peer(c)
        if len(chats) == 1:
            return utils.get_input_peer(chats[0])
        raise UserbotError("The userbot joined, but Telegram did not tell me which channel it is. Try again.")

    async def _peer_when_already_in(self, h: str):
        ub = self.ub.client
        try:
            info = await ub(functions.messages.CheckChatInviteRequest(h))
            if isinstance(info, types.ChatInviteAlready):
                return utils.get_input_peer(info.chat)
        except errors.RPCError:
            pass
        async for d in ub.iter_dialogs():
            if getattr(d.entity, "id", None) == self.ch.id and isinstance(d.entity, types.Channel):
                return utils.get_input_peer(d.entity)
        raise UserbotError("The userbot is in the channel, but I could not find it in its chat list.")

    async def _join_public(self, username: str):
        ub = self.ub.client
        try:
            peer = await ub.get_input_entity(username)
            try:
                await flood_retry(lambda: ub(functions.channels.JoinChannelRequest(peer)))
            except errors.UserAlreadyParticipantError:
                pass
        except (errors.ChannelsTooMuchError, errors.UserChannelsTooMuchError):
            raise UserbotError("The userbot account is in too many channels. Leave a few with that account and try again.")
        except errors.UserBannedInChannelError:
            raise UserbotError("The userbot account is banned in this channel. Unban it first.")
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, errors.ChannelPrivateError):
            raise UserbotError(f"The userbot could not open @{username}. Has the channel's name changed?")
        return peer

    # ------------------------------------------------------------------ rights
    async def _userbot_status(self):
        """(is the creator, its admin rights or None, its rank) as the channel shows the userbot; None if unknown."""
        try:
            res = await self.ub.client(functions.channels.GetParticipantRequest(self.peer, types.InputPeerSelf()))
        except errors.RPCError:
            return None
        p = res.participant
        if isinstance(p, types.ChannelParticipantCreator):
            return True, None, ""
        if isinstance(p, types.ChannelParticipantAdmin):
            return False, p.admin_rights, getattr(p, "rank", None) or ""
        return False, None, ""

    async def _userbot_has_right(self) -> bool:
        status = await self._userbot_status()
        if status is None:
            return False
        creator, rights, _ = status
        return creator or bool(getattr(rights, self.RIGHT, False))

    async def _ensure_right(self) -> None:
        if await self._userbot_has_right():
            return
        rights = await get_rights(self.ub.bot, self.ch)
        if rights is None or not (rights.admin and rights.add_admins):
            raise UserbotError(
                f"The userbot is in the channel but may not {self.WHAT} there, and the bot may not add admins. "
                "Give the bot the “Add new admins” right (it then promotes the userbot by itself), or make the userbot "
                f"an admin with “{self.RIGHT_NAME}” yourself."
            )
        user = await self._bot_view_of_userbot()
        status = await self._userbot_status()
        had = _rights_dict(status[1]) if status else {}
        rank = status[2] if status else ""
        try:
            await self.ub.bot(
                functions.channels.EditAdminRequest(
                    channel=peer_of(self.ch),
                    user_id=user,
                    admin_rights=types.ChatAdminRights(**{**had, self.RIGHT: True}),  # what it had, plus this one right
                    rank=rank,
                )
            )
        except errors.RPCError as e:
            name = type(e).__name__
            hint = {
                "RightForbiddenError": "The bot may not give that right.",
                "UserAdminInvalidError": "The bot may not change that user's admin status.",
                "AdminsTooMuchError": "The channel already has the maximum number of admins.",
                "ChatAdminRequiredError": "The bot needs the “Add new admins” right.",
            }.get(name, str(e)[:150])
            raise UserbotError(f"I could not make the userbot an admin ({name}). {hint}")
        self._promoted, self._bot_user = True, user
        if any(had.values()):
            self._before = (status[1], rank)
            self.notes.append(f"gave the userbot the “{self.RIGHT_NAME}” right on top of the ones it had")
        else:
            self.notes.append(f"made the userbot an admin with only “{self.RIGHT_NAME}”")
        for _ in range(12):  # the new right can take a moment to show up for the other account
            if await self._userbot_has_right():
                return
            await asyncio.sleep(0.5)
        raise UserbotError(
            f"I made the userbot an admin, but Telegram still does not let it {self.VERB} there. Check its rights."
        )

    async def _bot_view_of_userbot(self):
        """The userbot account as an InputUser the BOT can use (needs the bot's own access hash for that user)."""
        bot, me = self.ub.bot, self.ub.me
        uid = me.id
        try:
            ent = await bot.get_input_entity(uid)
            if isinstance(ent, types.InputPeerUser):
                return types.InputUser(ent.user_id, ent.access_hash)
        except Exception:
            pass
        queries = [types.ChannelParticipantsRecent()]
        for q in (getattr(me, "username", None), getattr(me, "first_name", None)):
            if q:
                queries.append(types.ChannelParticipantsSearch(q))
        for flt in queries:
            try:
                res = await bot(
                    functions.channels.GetParticipantsRequest(channel=peer_of(self.ch), filter=flt, offset=0, limit=200, hash=0)
                )
            except errors.RPCError:
                continue
            for u in getattr(res, "users", None) or []:
                if getattr(u, "id", None) == uid and getattr(u, "access_hash", None) is not None:
                    return types.InputUser(u.id, u.access_hash)
        if getattr(me, "username", None):
            try:
                ent = await bot.get_input_entity(me.username)
                if isinstance(ent, types.InputPeerUser):
                    return types.InputUser(ent.user_id, ent.access_hash)
            except Exception:
                pass
        raise UserbotError(
            "The userbot joined, but the bot cannot find that account in the channel's member list, so it cannot "
            f"promote it. Make the userbot an admin with “{self.RIGHT_NAME}” yourself."
        )


class UserbotDeleter(_ChannelJob):
    """Deletes messages in ONE channel with the userbot. Call close() when done, whatever happened."""

    async def delete(self, ids: list) -> None:
        await self._begin()
        client = self.ub.client
        for i in range(0, len(ids), 100):
            part = list(ids[i : i + 100])
            await flood_retry(
                lambda part=part: client(functions.channels.DeleteMessagesRequest(channel=self.peer, id=part))
            )


_EDIT_HINTS = {
    "ChatAdminRequiredError": "It needs the “Edit messages of others” admin right in that channel.",
    "MessageAuthorRequiredError": "It needs the “Edit messages of others” admin right in that channel.",
    "MessageIdInvalidError": "The post may be gone, or Telegram does not let anyone edit it.",
    "InlineBotRequiredError": "That post was sent through another bot's inline mode, so only that bot can edit it.",
    "MessageEditTimeExpiredError": "Telegram no longer allows editing that message.",
}


class UserbotEditor(_ChannelJob):
    """Edits posts in ONE channel as the userbot. Call close() when done, whatever happened.

    The userbot is a user account: it can't set inline buttons, so its edit carries none and Telegram may drop the ones
    the post had. The caller puts them back afterwards."""

    RIGHT = "edit_messages"
    RIGHT_NAME = "Edit messages of others"
    WHAT = "edit other people's posts"
    VERB = "edit"

    def __init__(self, ub: Userbot, ch):
        super().__init__(ub, ch)
        self._handed: list = []  # (the userbot account as the bot addresses it, the bot's id of the message): files put in its chat
        self.dm_problem: Optional[str] = None  # why a file could not go through that chat (None: it did, or it was not tried)

    async def close(self) -> None:
        """Clean up the channel (rights, link) and the files the bot put into the userbot's chat with the bot."""
        handed, self._handed = self._handed, []
        for peer, message_id in handed:
            try:
                await self.ub.bot.delete_messages(peer, [message_id])
            except Exception as e:  # noqa: BLE001 - at worst a message is left in the helper account's chat
                log.info("could not delete the helper message in the userbot's chat: %s", type(e).__name__)
        await super().close()

    async def edit(self, message_id: int, *, text: str, entities: list, media=None, preview=None) -> None:
        """Make the post show `text` (with `entities`) and, when given, `media`: what media_by_reference() returned (the
        file does not move at all) or a MediaFile (uploaded again)."""
        await self._begin()
        client = self.ub.client
        try:
            if isinstance(media, MediaFile):
                media = await self._upload(media)
            await flood_retry(
                lambda: edit_raw(client, self.peer, message_id, text=text, entities=entities, preview=preview, media=media)
            )
        except errors.MessageNotModifiedError:
            return  # it already looks like that
        except errors.RPCError as e:
            name = type(e).__name__
            raise UserbotRefused(
                f"Telegram refused the userbot as well ({name}). {_EDIT_HINTS.get(name) or str(e)[:200]}", name
            )

    # ----------------------------------------------------------- a file by way of the userbot's chat with the bot
    async def media_by_reference(self, ref: str):
        """The new photo / file as the userbot's own account holds it, ready to edit the post with - or None.

        The bot sends it to the userbot's chat with the bot (Telegram reuses the file it already has, nothing is
        downloaded or uploaded) and the userbot picks the message up there. The access data of a file belongs to one
        account, so the bot's own reference would be useless to the userbot: this is how the userbot gets its own.
        None when it does not work; `dm_problem` says why, and the caller uploads the file instead."""
        await self._begin()
        self.dm_problem = None
        try:
            return await self._hand_over(ref)
        except (errors.RPCError, ValueError, TypeError, OSError) as e:
            self.dm_problem = f"it failed with {type(e).__name__}"
            log.info("could not hand the file to the userbot: %s", type(e).__name__, exc_info=True)
            return None

    async def _hand_over(self, ref: str):
        bot, client = self.ub.bot, self.ub.client
        want = media_key(media_from_ref(ref))
        if want is None:
            raise ValueError("not a photo or a file")
        try:  # both ends of the chat first: nothing is sent when one of them can't be found
            user = await self._bot_view_of_userbot()
        except UserbotError:
            self.dm_problem = "the bot cannot find the userbot account"
            return None
        try:
            bot_peer = await self._userbot_view_of_bot()
        except UserbotError as e:
            self.dm_problem = str(e)
            return None
        peer = types.InputPeerUser(user.user_id, user.access_hash)
        try:
            sent = await with_media(ref, lambda media: flood_retry(lambda: bot.send_file(peer, media, silent=True)))
        except errors.RPCError as e:
            name = type(e).__name__
            self.dm_problem = (
                "the userbot account has not started the bot yet (send /start to the bot from that account)"
                if name in _NOT_STARTED
                else f"Telegram would not let the bot send the file to the userbot ({name})"
            )
            return None
        self._handed.append((peer, sent.id))  # deleted again in close()
        for i in range(_DM_POLLS):  # the message has to reach the userbot's side first
            if i:
                await asyncio.sleep(_DM_WAIT)
            try:
                msgs = await flood_retry(lambda: client.get_messages(bot_peer, limit=10))
            except errors.RPCError as e:
                self.dm_problem = f"the userbot could not read its chat with the bot ({type(e).__name__})"
                return None
            for m in msgs:  # newest first; the same photo / file has the same id on both accounts
                if media_key(getattr(m, "media", None)) == want:
                    return utils.get_input_media(m.media)
        self.dm_problem = "the file did not show up in the userbot's chat with the bot"
        return None

    async def _userbot_view_of_bot(self):
        """The bot as the userbot account addresses it (found by the bot's username)."""
        me = await self.ub.bot.get_me()
        name = getattr(me, "username", None)
        if not name:
            raise UserbotError("the bot has no username, so the userbot cannot find its chat with it")
        try:
            return await self.ub.client.get_input_entity(name)
        except (errors.RPCError, ValueError):
            raise UserbotError("the userbot could not find the bot by its username")

    # --------------------------------------------------------------------------------- the slow way
    async def _upload(self, f: MediaFile):
        """Upload a downloaded photo / file (with its thumbnail) as the new media of the post."""
        client = self.ub.client
        handle = await flood_retry(lambda: client.upload_file(f.path, file_name=f.name))
        if f.kind == "photo":
            return types.InputMediaUploadedPhoto(file=handle)
        thumb = None
        if f.thumb_path:
            try:
                thumb = await flood_retry(lambda: client.upload_file(f.thumb_path))
            except (errors.RPCError, OSError) as e:  # the file goes without it rather than not at all
                log.info("could not upload the thumbnail: %s", type(e).__name__)
        return types.InputMediaUploadedDocument(
            file=handle,
            mime_type=f.mime_type or "application/octet-stream",
            attributes=list(f.attributes),
            thumb=thumb,
            force_file=True if f.kind == "document" else None,  # a file sent "as a file" stays one
        )


def delete_problem_text(ub, *, bot_error: Optional[str] = None, fallback_error: Optional[str] = None, tried: bool = False) -> str:
    """Why some messages could not be deleted, in words for the owner."""
    parts = []
    if bot_error:
        parts.append(f"Telegram refused the bot ({bot_error}).")
    if fallback_error:
        parts.append(f"The userbot could not help: {fallback_error}")
    elif tried:
        parts.append("The userbot tried as well, but the posts are still there.")
    elif ub is None or not getattr(ub, "enabled", False):
        parts.append("Posts older than 48 hours may need a userbot - see /userbot.")
    elif not ub.ready:
        parts.append(f"The userbot is not connected: {ub.last_error or 'unknown reason'}")
    return " ".join(parts) or "Telegram did not delete them (the bot's “Delete messages” right, or an age limit on old posts)."


def edit_problem_text(ub, *, fallback_error: Optional[str] = None) -> str:
    """What to add when Telegram refused the bot's edit and the userbot could not take over (empty: nothing to add)."""
    if fallback_error:
        return f"The userbot could not do it either: {fallback_error}"
    if ub is None or not getattr(ub, "enabled", False):
        return "A userbot can edit such posts for you - see /userbot."
    if not ub.ready:
        return f"The userbot is not connected: {ub.last_error or 'unknown reason'}"
    return ""
