"""/shift -all: the destination channel gets the name, description and profile photo of the source channel.

Each part is done on its own, so one that fails (a missing admin right, a photo Telegram refuses) does not stop the
others. A part the source does not have (no description, no photo) is left as it is in the destination: nothing is ever
wiped. The notices Telegram adds to a channel when its name or photo changes are deleted again right away.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

from telethon import errors, functions, types

from .repost_engine import delete_ids, new_messages_from
from .tgutil import esc, flood_retry, peer_of

log = logging.getLogger(__name__)

PARTS = ("name", "description", "photo")
LABELS = {"name": "Name", "description": "Description", "photo": "Profile photo"}
NEEDS_RIGHT = "the bot needs the “Change channel info” admin right in the destination"


@dataclass
class ProfileResult:
    """What happened to each part: part -> (status, detail). Status: done | same | none | failed."""

    outcome: dict = field(default_factory=dict)
    title: Optional[str] = None  # the destination's name after the run (None: it did not change)

    @property
    def ok(self) -> bool:
        return all(s != "failed" for s, _ in self.outcome.values())

    @property
    def done(self) -> list:
        return [p for p in PARTS if self.outcome.get(p, ("", ""))[0] == "done"]

    @property
    def failed(self) -> list:
        return [(p, d) for p in PARTS for s, d in [self.outcome.get(p, ("", ""))] if s == "failed"]


def _real(photo):
    return None if photo is None or isinstance(photo, types.PhotoEmpty) else photo


async def read_profile(client, ch) -> SimpleNamespace:
    """The name, the description and the profile photo a channel shows right now."""
    full = await flood_retry(lambda: client(functions.channels.GetFullChannelRequest(peer_of(ch))))
    chat = next((c for c in getattr(full, "chats", None) or [] if getattr(c, "id", None) == ch.id), None)
    fc = full.full_chat
    return SimpleNamespace(
        title=getattr(chat, "title", None), about=getattr(fc, "about", None) or "", photo=_real(getattr(fc, "chat_photo", None))
    )


def _failure(e: BaseException) -> str:
    name = type(e).__name__
    if name == "ChatAdminRequiredError":
        return NEEDS_RIGHT
    return name


async def _set_name(client, dst, title: str) -> tuple:
    try:
        out = await flood_retry(lambda: client(functions.channels.EditTitleRequest(channel=peer_of(dst), title=title)))
    except errors.ChatNotModifiedError:
        return "same", ""
    except errors.RPCError as e:
        return "failed", _failure(e)
    await _drop_notices(client, dst, out)
    return "done", title


async def _set_description(client, dst, about: str) -> tuple:
    try:
        await flood_retry(lambda: client(functions.messages.EditChatAboutRequest(peer=peer_of(dst), about=about)))
    except (errors.ChatAboutNotModifiedError, errors.ChatNotModifiedError):
        return "same", ""
    except errors.RPCError as e:
        return "failed", _failure(e)
    return "done", ""


async def _set_photo(client, dst, photo) -> tuple:
    try:
        data = await client.download_media(photo, file=bytes)
        if not data:
            return "failed", "the photo could not be downloaded"
        uploaded = await client.upload_file(data, file_name="channel_photo.jpg")
        out = await flood_retry(
            lambda: client(
                functions.channels.EditPhotoRequest(channel=peer_of(dst), photo=types.InputChatUploadedPhoto(file=uploaded))
            )
        )
    except errors.ChatNotModifiedError:
        return "same", ""
    except errors.RPCError as e:
        return "failed", _failure(e)
    except Exception as e:  # a download or upload problem is not a reason to stop the shift
        log.warning("copying the profile photo failed: %s %s", type(e).__name__, e)
        return "failed", type(e).__name__
    await _drop_notices(client, dst, out)
    return "done", ""


async def _drop_notices(client, ch, result) -> None:
    """Remove the "channel name / photo changed" notices Telegram put into the channel."""
    ids = [m.id for m in new_messages_from(result) if isinstance(m, types.MessageService)]
    if ids:
        await delete_ids(client, peer_of(ch), ids)


async def copy_profile(client, src, dst) -> ProfileResult:
    """Give `dst` the name, the description and the profile photo of `src`. Nothing is wiped: a part that the source
    does not have stays as the destination has it. Both arguments need `id` and `access_hash`."""
    res = ProfileResult()
    try:
        s = await read_profile(client, src)
    except errors.RPCError as e:
        for p in PARTS:
            res.outcome[p] = ("failed", f"the source can't be read ({type(e).__name__})")
        return res
    try:
        d = await read_profile(client, dst)
    except errors.RPCError:
        d = SimpleNamespace(title=None, about=None, photo=None)  # unknown: every part is simply tried

    if not s.title:
        res.outcome["name"] = ("none", "")
    elif s.title == d.title:
        res.outcome["name"] = ("same", "")
    else:
        res.outcome["name"] = await _set_name(client, dst, s.title)
        if res.outcome["name"][0] == "done":
            res.title = s.title

    if not s.about:
        res.outcome["description"] = ("none", "")
    elif s.about == d.about:
        res.outcome["description"] = ("same", "")
    else:
        res.outcome["description"] = await _set_description(client, dst, s.about)

    if s.photo is None:
        res.outcome["photo"] = ("none", "")
    else:
        res.outcome["photo"] = await _set_photo(client, dst, s.photo)
    return res


def profile_lines(res: ProfileResult, dst_title: str = "") -> list:
    """Lines (HTML) about what was copied, for the end of a shift."""
    shown = {"done": "copied", "same": "already the same", "none": "the source has none - left as it is"}
    parts, bad = [], []
    for p in PARTS:
        status, detail = res.outcome.get(p, ("", ""))
        if status == "failed":
            bad.append(f"{LABELS[p]}: {esc(detail)}")
        elif status:
            parts.append(f"{LABELS[p].lower()} {shown[status]}")
    out = []
    if parts:
        out.append("🖼 " + ("<b>" + esc(dst_title) + "</b>: " if dst_title else "") + ", ".join(parts) + ".")
    if bad:
        out.append("⚠️ Not copied - " + "; ".join(bad) + ".")
    return out
