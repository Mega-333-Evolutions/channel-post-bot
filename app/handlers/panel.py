"""Helpers shared by the create and post-management handlers (not a handler module itself)."""
from __future__ import annotations

from telethon import types

from ..buttons import valid
from ..common import Ctx, UserError, is_cb, say, show
from ..tgutil import classify_media, pack_file_id, ser_entities, utf16_len
from ..ui import button_keyboard, button_text, panel_keyboard, panel_text

CAPTION_MAX = 1024
TEXT_MAX = 4096


async def load_post(ctx: Ctx, pid):
    post = await ctx.db.get_post(int(pid))
    if post is None:
        raise UserError("That post no longer exists.")
    ch = await ctx.db.get_channel(post.channel_id)
    if ch is None:
        raise UserError("Its channel was removed from the bot. Add it again with /addchannel.")
    return post, ch


async def show_panel(ctx: Ctx, event, pid, note=None, new: bool = False) -> None:
    post, ch = await load_post(ctx, pid)
    text, kb = panel_text(post, ch, note), panel_keyboard(post)
    if new or not is_cb(event):
        await say(event, text, kb)
    else:
        await show(event, text, kb)


async def show_button_screen(ctx: Ctx, event, pid, r: int, c: int, new: bool = False) -> None:
    post, _ = await load_post(ctx, pid)
    if not valid(post.buttons or [], r, c):
        await show_panel(ctx, event, pid, new=new)
        return
    text, kb = button_text(post, r, c), button_keyboard(post, r, c)
    if new or not is_cb(event):
        await say(event, text, kb)
    else:
        await show(event, text, kb)


def check_lengths(text: str, has_media: bool) -> None:
    n = utf16_len(text or "")
    if has_media and n > CAPTION_MAX:
        raise UserError(f"A caption can have at most {CAPTION_MAX} characters (yours has {n}).")
    if not has_media and n > TEXT_MAX:
        raise UserError(f"A text post can have at most {TEXT_MAX} characters (yours has {n}).")


def extract_content(m) -> dict:
    """A message from the user -> post fields (text, entities, media)."""
    kind = classify_media(m)
    if m.media and kind is None and not isinstance(m.media, types.MessageMediaWebPage):
        raise UserError("That kind of message can't be used in a post. Send text, a photo, video, GIF, audio or a file.")
    text = m.raw_text or ""
    if not kind and not text.strip():
        raise UserError("Send some text, or a photo / video / GIF / file.")
    check_lengths(text, bool(kind))
    return {
        "text": text,
        "entities": ser_entities(m.entities),
        "media_kind": kind,
        "media_file_id": pack_file_id(m) if kind else None,
    }
