"""Find a channel from what a person types: @username, t.me link, numeric id (-100...), or an invite link.

Used by /shift. The bot can only talk to a channel it can see: registered channels (the database has their access
details), channels it has met in updates, public channels by @username, and channels it is a member of (invite link).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from telethon import functions, types

from .common import Ctx, UserError

_REF_RX = re.compile(r"(?i)^(?:https?://)?(?:t\.me|telegram\.me)/(?:c/(\d+)|([A-Za-z0-9_]{3,32}))(?:/\d+)?/?$")
_INVITE_RX = re.compile(r"(?i)^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([A-Za-z0-9_-]{6,})/?$")
_TG_INVITE_RX = re.compile(r"(?i)^tg://join\?invite=([A-Za-z0-9_-]{6,})$")


def parse_channel_ref(text: str):
    """@name / t.me link / numeric id -> username (str) or channel id (int); None if unrecognised."""
    t = (text or "").strip()
    m = _REF_RX.match(t)
    if m:
        return int(m.group(1)) if m.group(1) else m.group(2)
    t = t.lstrip("@")
    if re.fullmatch(r"-?\d+", t):
        s = str(abs(int(t)))
        return int(s[3:]) if s.startswith("100") and len(s) > 10 else int(s)
    if re.fullmatch(r"[A-Za-z0-9_]{3,32}", t):
        return t
    return None


def classify_ref(text: str) -> tuple:
    """-> ("invite", hash) | ("username", name) | ("id", int) | (None, None)"""
    t = (text or "").strip()
    m = _INVITE_RX.match(t) or _TG_INVITE_RX.match(t)
    if m:
        return "invite", m.group(1)
    ref = parse_channel_ref(t)
    if isinstance(ref, int):
        return "id", ref
    if isinstance(ref, str):
        return "username", ref
    return None, None


@dataclass
class ChannelRef:
    """A channel as the bot can reach it."""

    id: int
    access_hash: int
    title: str
    username: Optional[str]
    registered: bool  # the bot's database knows it (it can be used with /new, /posts ...)


def _from_entity(ent, registered: bool) -> ChannelRef:
    if not isinstance(ent, types.Channel):
        raise UserError("That is not a channel (it looks like a user, bot or group). Only channels can be used here.")
    if not ent.broadcast:
        raise UserError("That is a group, not a channel. Only channels can be used here.")
    if not ent.access_hash or getattr(ent, "min", False):
        raise UserError(
            "I can see that channel, but Telegram has not given me its access details. Use its @username, or add the "
            "bot to the channel as an admin and post something there first."
        )
    return ChannelRef(ent.id, ent.access_hash, ent.title or "", ent.username, registered)


async def _entity(client, what):
    try:
        return await client.get_entity(what)
    except Exception:
        return None


async def resolve_channel(ctx: Ctx, text: str) -> ChannelRef:
    """The channel `text` points at, or a UserError that says what to do."""
    kind, value = classify_ref(text)
    if kind is None:
        raise UserError(
            f"“{text}” is not a channel I understand. Use an @username, a t.me link, the channel id (-100…) or an invite link."
        )
    db, client = ctx.db, ctx.client
    if kind == "username":
        for ch in await db.list_channels(active_only=False):
            if ch.username and ch.username.lower() == value.lower():
                return ChannelRef(ch.id, ch.access_hash, ch.title, ch.username, ch.active)
        ent = await _entity(client, value)
        if ent is None:
            raise UserError(f"I couldn't find a channel called @{value}. Check the name, or add the bot to it as an admin.")
    elif kind == "id":
        ch = await db.get_channel(value)
        if ch is not None:
            return ChannelRef(ch.id, ch.access_hash, ch.title, ch.username, ch.active)
        ent = ctx.seen_channels.get(value) or await _entity(client, types.PeerChannel(value))
        if ent is None:
            raise UserError(
                f"I don't know a channel with the id {value}. A bot can only use an id for channels it is part of; "
                "use the @username instead, or add the bot to the channel as an admin."
            )
    else:  # invite link: only tells which channel it is; the bot cannot join by itself
        try:
            info = await client(functions.messages.CheckChatInviteRequest(hash=value))
        except Exception as e:
            raise UserError(f"That invite link doesn't work ({type(e).__name__}).")
        chat = getattr(info, "chat", None)
        if chat is None:
            raise UserError(
                "The bot is not a member of that channel, so it cannot use that invite link. Add the bot to the channel "
                "as an admin first (a bot cannot join by itself)."
            )
        ent = chat
    known = await db.get_channel(getattr(ent, "id", 0))
    return _from_entity(ent, known is not None and known.active)
