"""Service messages: the notices Telegram puts into a channel by itself - "channel name changed", "channel photo
updated", "<admin> pinned a message", "live stream started" ... The bot removes them from every connected channel.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from telethon import errors, functions, types

from .tgutil import flood_retry

log = logging.getLogger(__name__)

# Telegram refusals that mean "the bot has no right to delete here" (worth telling the owner about)
RIGHTS_ERRORS = ("ChatAdminRequiredError", "ChatWriteForbiddenError", "ChannelPrivateError", "UserNotParticipantError")


def is_service(m) -> bool:
    return isinstance(m, types.MessageService)


def action_label(m) -> str:
    """'PinMessage', 'ChatEditTitle', 'GroupCall' ... - for the log."""
    name = type(getattr(m, "action", None)).__name__
    return name[len("MessageAction"):] if name.startswith("MessageAction") else name


@dataclass
class Removed:
    deleted: int = 0
    failed: int = 0
    error: Optional[BaseException] = None  # the first thing Telegram refused with

    @property
    def error_name(self) -> Optional[str]:
        return type(self.error).__name__ if self.error is not None else None


async def remove(client, peer, ids: list) -> Removed:
    """Delete these messages, 100 per request. A refusal is reported in the result, never raised."""
    out = Removed()
    ids = sorted(set(ids))
    for i in range(0, len(ids), 100):
        part = ids[i : i + 100]
        try:
            res = await flood_retry(lambda part=part: client(functions.channels.DeleteMessagesRequest(channel=peer, id=part)))
        except errors.RPCError as e:
            out.failed += len(part)
            out.error = out.error or e
            continue
        n = getattr(res, "pts_count", None)
        out.deleted += len(part) if n is None else min(len(part), int(n))
    return out
