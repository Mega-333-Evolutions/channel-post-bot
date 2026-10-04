"""A userbot - a normal Telegram account - that deletes the posts the bot is not allowed to delete.

Telegram's documentation says bots can only delete messages younger than 48 hours. The bot always tries first;
whatever is left goes to the userbot:
  1. the bot makes a one-time invite link (or reads the channel's own link) and the userbot joins with it
     (public channels without a usable link: the userbot joins by name);
  2. if the userbot is not allowed to delete there yet and the bot has the "Add new admins" right, the bot makes
     the userbot an admin with only "Delete messages";
  3. the userbot deletes, and afterwards the bot takes the admin right away again (USERBOT_KEEP_ADMIN=true keeps it).
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

from .tgutil import flood_retry, get_rights, peer_of

log = logging.getLogger(__name__)

_INVITE_RX = re.compile(
    r"(?i)(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([A-Za-z0-9_-]{8,})"
    r"|tg://join\?invite=([A-Za-z0-9_-]{8,})"
)


class UserbotError(Exception):
    """Something the owner can fix; the text is meant to be shown."""


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
        self.lock = asyncio.Lock()  # one channel clean-up at a time (join / promote / delete / demote)

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


class UserbotDeleter:
    """Everything needed to delete in ONE channel with the userbot. Call close() when done, whatever happened."""

    def __init__(self, ub: Userbot, ch):
        self.ub = ub
        self.ch = ch
        self.peer = None  # the channel as the userbot sees it
        self.notes: list = []
        self._ready = False
        self._holds_lock = False
        self._promoted = False
        self._bot_user = None  # the userbot account as the bot sees it (InputUser)
        self._link: Optional[str] = None  # the temporary invite link we made

    # ------------------------------------------------------------------ public
    async def delete(self, ids: list) -> None:
        if not self.ub.ready:
            raise UserbotError(f"The userbot is not connected: {self.ub.last_error or 'unknown reason'}")
        if not self._ready:
            if not self._holds_lock:  # released again in close()
                await self.ub.lock.acquire()
                self._holds_lock = True
            await self._prepare()
            self._ready = True
        client = self.ub.client
        for i in range(0, len(ids), 100):
            part = list(ids[i : i + 100])
            await flood_retry(
                lambda part=part: client(functions.channels.DeleteMessagesRequest(channel=self.peer, id=part))
            )

    async def close(self) -> None:
        """Undo what this clean-up changed: take the admin right away again, revoke the temporary link."""
        bot, ch = self.ub.bot, self.ch
        try:
            if self._promoted and self._bot_user is not None and not self.ub.keep_admin:
                try:
                    await bot(
                        functions.channels.EditAdminRequest(
                            channel=peer_of(ch), user_id=self._bot_user, admin_rights=types.ChatAdminRights(), rank=""
                        )
                    )
                    self.notes.append("took the admin right away from the userbot again")
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
        await self._ensure_delete_right()

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
            "(with the “Delete messages” admin right)."
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
    async def _userbot_can_delete(self) -> bool:
        try:
            res = await self.ub.client(functions.channels.GetParticipantRequest(self.peer, types.InputPeerSelf()))
        except errors.RPCError:
            return False
        p = res.participant
        if isinstance(p, types.ChannelParticipantCreator):
            return True
        if isinstance(p, types.ChannelParticipantAdmin):
            return bool(getattr(p.admin_rights, "delete_messages", False))
        return False

    async def _ensure_delete_right(self) -> None:
        if await self._userbot_can_delete():
            return
        rights = await get_rights(self.ub.bot, self.ch)
        if rights is None or not (rights.admin and rights.add_admins):
            raise UserbotError(
                "The userbot is in the channel but may not delete messages there, and the bot may not add admins. "
                "Give the bot the “Add new admins” right (it then promotes the userbot by itself), or make the userbot "
                "an admin with “Delete messages” yourself."
            )
        user = await self._bot_view_of_userbot()
        try:
            await self.ub.bot(
                functions.channels.EditAdminRequest(
                    channel=peer_of(self.ch),
                    user_id=user,
                    admin_rights=types.ChatAdminRights(delete_messages=True),
                    rank="",
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
        self.notes.append("made the userbot an admin with only “Delete messages”")
        for _ in range(12):  # the new right can take a moment to show up for the other account
            if await self._userbot_can_delete():
                return
            await asyncio.sleep(0.5)
        raise UserbotError("I made the userbot an admin, but Telegram still does not let it delete there. Check its rights.")

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
            "promote it. Make the userbot an admin with “Delete messages” yourself."
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
