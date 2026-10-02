"""Shared context, permission guard, message helpers and post send/edit operations."""
from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from telethon import TelegramClient, errors, events

from .config import Config
from .db import Database
from .tgutil import build_markup, de_entities, edit_raw, esc, explain_rpc, peer_of, with_media

log = logging.getLogger(__name__)


class UserError(Exception):
    """An error whose message is safe and useful to show to the user."""


@dataclass
class Ctx:
    cfg: Config
    db: Database
    client: TelegramClient
    state: dict = field(default_factory=dict)  # user id -> {"mode": ..., ...}
    pending: dict = field(default_factory=dict)  # tokens for confirm dialogs (replace, test edit)
    seen_channels: dict = field(default_factory=dict)  # channel id -> Channel entity seen in updates
    text_handlers: dict = field(default_factory=dict)  # mode -> async fn(event, state)
    callbacks: dict = field(default_factory=dict)  # prefix -> async fn(event, parts)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one long job (replace / repost) at a time


def is_cb(event) -> bool:
    return isinstance(event, events.CallbackQuery.Event) or getattr(event, "_is_callback", False)


def guard(ctx: Ctx, owner: bool = False) -> Callable:
    """Only allowed users may use a handler; errors are turned into friendly messages."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(event):
            uid = event.sender_id
            ok = ctx.cfg.is_owner(uid) if owner else ctx.cfg.is_allowed(uid)
            if not ok:
                try:
                    if is_cb(event):
                        await event.answer("Not allowed.", alert=True)
                    elif getattr(event, "is_private", False):
                        await event.respond("⛔ This bot is private.")
                except Exception:
                    pass
                return
            try:
                await fn(event)
            except events.StopPropagation:
                raise
            except errors.MessageNotModifiedError:
                if is_cb(event):
                    try:
                        await event.answer()
                    except Exception:
                        pass
            except UserError as e:
                await notify_error(event, str(e))
            except errors.RPCError as e:
                await notify_error(event, "❌ " + explain_rpc(e))
            except Exception as e:
                log.exception("handler %s failed", fn.__name__)
                await notify_error(event, f"⚠️ Something went wrong: {type(e).__name__}: {str(e)[:200]}")

        return wrapper

    return deco


async def notify_error(event, text: str) -> None:
    try:
        if is_cb(event):
            try:
                await event.answer("That didn't work - see the message below.", alert=False)
            except Exception:
                pass
        await event.respond(esc(text), link_preview=False)
    except Exception:  # pragma: no cover
        log.exception("could not report an error")


async def say(event, text: str, buttons=None):
    return await event.respond(text, buttons=buttons, link_preview=False)


async def edit_callback_message(event, text: str, buttons=None):
    """Edit the message a button was pressed on.

    Telethon's CallbackQuery.edit() quietly starts a second `answer` task; the dispatcher has already
    answered the query, so we call edit_message directly instead.
    """
    query = getattr(event, "query", None)
    if query is not None and hasattr(event, "client"):
        return await event.client.edit_message(
            await event.get_input_chat(), query.msg_id, text, buttons=buttons, link_preview=False
        )
    return await event.edit(text, buttons=buttons, link_preview=False)  # test doubles


async def show(event, text: str, buttons=None):
    """Edit the message a button was pressed on; for normal messages send a new one."""
    if is_cb(event):
        try:
            return await edit_callback_message(event, text, buttons)
        except errors.MessageNotModifiedError:
            return None
    return await say(event, text, buttons)


def require_owner(ctx: Ctx, event) -> None:
    if not ctx.cfg.is_owner(event.sender_id):
        raise UserError("⛔ Only the bot owner can do that.")


# ------------------------------------------------------------------ sending and editing
async def send_post(client, peer, post):
    ents = de_entities(post.entities)
    markup = build_markup(post.buttons)
    if post.media_file_id:
        return await with_media(
            post.media_file_id,
            lambda media: client.send_file(
                peer, media, caption=post.text or "", formatting_entities=ents, buttons=markup
            ),
        )
    return await client.send_message(
        peer, post.text, formatting_entities=ents, buttons=markup, link_preview=bool(post.link_preview)
    )


async def push_edit(client, ch, p, *, replace_media: bool = False) -> None:
    """Make the live channel message look like `p` (any object with the Post fields)."""
    peer = peer_of(ch)
    ents = de_entities(p.entities)
    markup = build_markup(p.buttons)
    if replace_media and p.media_file_id:
        await with_media(
            p.media_file_id,
            lambda media: client.edit_message(
                peer, p.message_id, text=p.text or "", formatting_entities=ents, buttons=markup, file=media
            ),
        )
    else:
        await edit_raw(
            client,
            peer,
            p.message_id,
            text=p.text or "",
            entities=ents,
            markup=markup,
            preview=None if p.media_kind else bool(p.link_preview),
        )


async def apply_edit(ctx: Ctx, post, ch, *, replace_media: bool = False, **changes):
    """Edit the channel message first (if the post is published), and only then save the change."""
    if post.status == "sent" and post.message_id:
        new = SimpleNamespace(**{**post.as_dict(), **changes})
        try:
            await push_edit(ctx.client, ch, new, replace_media=replace_media)
        except errors.MessageNotModifiedError:
            pass
        except errors.RPCError as e:
            raise UserError("❌ " + explain_rpc(e) + "\nNothing was changed.")
    return await ctx.db.update_post(post.id, **changes)


def on_cb(ctx: Ctx, prefix: str):
    def deco(fn):
        ctx.callbacks[prefix] = fn
        return fn

    return deco


def on_text(ctx: Ctx, mode: str):
    def deco(fn):
        ctx.text_handlers[mode] = fn
        return fn

    return deco


def set_state(ctx: Ctx, uid: int, mode: str, **data: Any) -> dict:
    ctx.state[uid] = {"mode": mode, **data}
    return ctx.state[uid]


def clear_state(ctx: Ctx, uid: int) -> Optional[dict]:
    return ctx.state.pop(uid, None)


def cmd(name: str, args: bool = False):
    """Event builder for a private-chat command such as /new or /replace <args>."""
    pat = rf"(?is)^/{name}(?:@\w+)?" + (r"(?:\s+(.*))?$" if args else r"\s*$")
    return events.NewMessage(incoming=True, pattern=pat, func=lambda e: e.is_private)


async def discard_temp(ctx: Ctx, uid: int) -> None:
    """Drop a half-made draft when the user abandons the flow that was creating it."""
    st = ctx.state.get(uid)
    if st and st.get("temp") and st.get("pid"):
        p = await ctx.db.get_post(st["pid"])
        if p is not None and p.status == "draft":
            await ctx.db.delete_post(p.id)
