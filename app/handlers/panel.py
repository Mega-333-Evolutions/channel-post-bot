"""Helpers shared by the create and post-management handlers (not a handler module itself)."""
from __future__ import annotations

from typing import Optional

from telethon import types

from ..buttons import valid
from ..common import Ctx, UserError, is_cb, say, show
from ..richmsg import Rich, RichError, check as check_rich, entry as rich_entry, plain_of, read_typed, received
from ..richtext import message_text
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


class Content(dict):
    """Post fields (text, entities, media_kind, media_file_id).

    `how` says what the text was read as: "html" or "markdown" (turned into formatting), "rich" (a rich message sent
    natively, kept as it is), "rich-html" / "rich-markdown" (typed, and Telegram builds the blocks), or None."""

    how = None


def markup_note(how, sep: str = "\n\n") -> str:
    """A line for the owner when their text was read as HTML or Markdown, followed by `sep` (empty otherwise)."""
    if not how:
        return ""
    if how == "rich":
        return f"🧩 Rich message kept as it is - 👁 Preview shows it.{sep}"
    if how.startswith("rich-"):
        lang = "HTML" if how == "rich-html" else "Markdown"
        return f"🧩 Rich message read from {lang} - 👁 Preview shows what Telegram makes of it (/rich).{sep}"
    return f"🎨 Formatting read from {'HTML' if how == 'html' else 'Markdown'} - see /formatting.{sep}"


def is_rich_how(how) -> bool:
    return bool(how) and how.startswith("rich")


def read_text(m, markup: bool = True, rich: Optional[Rich] = None, allow_rich: bool = True) -> tuple:
    """(text, entities, how) of what the owner sent - see Content.how.

    A rich message (`rich`: sent natively, kept block for block) or text typed as HTML / Markdown that needs blocks
    becomes a rich post: `text` is its plain-text version and its only entity is the rich entry. Otherwise it is an
    ordinary text with Telegram's own formatting, or HTML / Markdown turned into formatting. A message that has
    Telegram formatting of its own is used as it is. `allow_rich` is False where a rich message can't be (a caption)."""
    how = None
    if rich is not None:
        if not allow_rich:
            raise UserError("A caption can't be a rich message. Rich messages are for text posts.")
        how = "rich"
    elif allow_rich and markup and not ser_entities(m.entities):
        rich = read_typed(m.raw_text or "")
        how = f"rich-{rich.fmt}" if rich is not None else None
    if rich is None:
        return message_text(m, markup)
    try:
        check_rich(rich)
    except RichError as e:
        raise UserError(str(e))
    return plain_of(rich), [rich_entry(rich)], how


async def read_native(ctx: Ctx, event) -> Optional[Rich]:
    """The rich message the owner sent natively (or forwarded), or None for an ordinary message."""
    try:
        return await received(ctx.client, event)
    except RichError as e:
        raise UserError(str(e))


def extract_content(m, markup: bool = True, rich: Optional[Rich] = None) -> Content:
    """A message from the user -> post fields (text, entities, media).

    Telegram's own formatting is kept as it is; without any, `markup` lets HTML or Markdown in the text become formatting
    (or a rich message, when it has blocks only those have). `rich`: the message is a rich message sent natively."""
    kind = classify_media(m)
    if m.media and kind is None and not isinstance(m.media, types.MessageMediaWebPage):
        raise UserError("That kind of message can't be used in a post. Send text, a photo, video, GIF, audio or a file.")
    text, entities, how = read_text(m, markup, rich if not kind else None, allow_rich=not kind)
    if not kind and not text.strip() and not is_rich_how(how):
        raise UserError("Send some text, or a photo / video / GIF / file.")
    if not is_rich_how(how):
        check_lengths(text, bool(kind))
    content = Content(text=text, entities=entities, media_kind=kind, media_file_id=pack_file_id(m) if kind else None)
    content.how = how
    return content


async def read_content(ctx: Ctx, event) -> Content:
    """extract_content for a message that just arrived (a rich message sent natively is read, in full, first)."""
    return extract_content(event.message, ctx.cfg.markup_input, await read_native(ctx, event))
