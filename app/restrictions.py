"""What TELEGRAM does to a channel (not what an admin does): the copyright notice and its relatives.

A channel that gets a copyright strike shows "This message couldn't be displayed on your device due to copyright
infringement." in place of its posts. That is not an edit by an admin, so nothing here ever treats it as one: the sync
leaves My posts as saved, and the link fixer does not edit such posts.
"""
from __future__ import annotations

import re
from typing import Optional

from telethon import errors, functions

from .tgutil import explain_rpc, flood_retry

PLACEHOLDER_NOTE = "Telegram shows a notice instead of the post"

# "This message couldn't be displayed on your device due to copyright infringement." and its relatives
_NOTICE = re.compile(
    r"^this (?:message|post|channel|media|content|file|video)\b.{0,80}?"
    r"\b(?:can.?t|cannot|can not|couldn.?t|could not|isn.?t|is not|won.?t|will not)\b.{0,40}?"
    r"\b(?:displayed|shown|viewed|available|accessible)\b.{0,160}$",
    re.I,
)
_NOTICE_WHY = re.compile(r"copyright|infring|device|country|region|terms of service|violat|restrict|\blaw\b", re.I)


def norm_text(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def is_placeholder_text(text: Optional[str]) -> bool:
    """The notice Telegram's apps show in place of a post it holds back (copyright, terms of service, region ...)."""
    t = norm_text(text)
    return 0 < len(t) <= 260 and bool(_NOTICE.match(t)) and bool(_NOTICE_WHY.search(t))


def restriction_of(obj) -> Optional[str]:
    """What Telegram says about a restricted channel or message (its own words), or None when nothing is restricted."""
    reasons = getattr(obj, "restriction_reason", None)
    if reasons:
        texts = [norm_text(str(getattr(r, "text", "") or getattr(r, "reason", "") or "")) for r in reasons]
        return "; ".join(dict.fromkeys(t for t in texts if t))[:240] or "restricted by Telegram"
    if getattr(obj, "restricted", None):
        return "restricted by Telegram"
    return None


def message_restriction(m, saved_text: Optional[str] = None) -> Optional[str]:
    """Why this message must not be taken over as it looks now, or None. Telegram marks a message it holds back
    (restriction_reason); some places hand out its notice as the text instead. `saved_text` is the saved copy's text: a
    text that the saved copy already has is the owner's own wording, not a notice."""
    why = restriction_of(m)
    if why:
        return why
    live = m.message or ""
    if is_placeholder_text(live) and (saved_text is None or norm_text(live) != norm_text(saved_text)):
        return PLACEHOLDER_NOTE
    return None


async def probe_channel(client, peer) -> tuple:
    """(the channel's event counter, why Telegram restricts the channel or None, None) - or (None, None, why not).
    The counter is never lower than the newest message id."""
    try:
        full = await flood_retry(lambda: client(functions.channels.GetFullChannelRequest(peer)))
    except errors.RPCError as e:
        return None, None, explain_rpc(e)
    pts = getattr(getattr(full, "full_chat", None), "pts", None)
    if pts is None:
        return None, None, "Telegram did not say how far the channel has come."
    cid = getattr(peer, "channel_id", None)
    why = None
    for chat in getattr(full, "chats", None) or []:
        if getattr(chat, "id", None) == cid:
            why = restriction_of(chat)
    return int(pts), why, None
