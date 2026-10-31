"""Shared context, permission guard, message helpers and post send/edit operations."""
from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from telethon import TelegramClient, errors, events

from .buttons_sync import ensure_markup, link_urls
from .config import Config
from .db import Database
from .replace_engine import fetch_messages, is_skippable
from .tgutil import build_markup, de_entities, edit_raw, esc, explain_rpc, fetch_media, media_info, peer_of, with_media
from .userbot import UserbotError, UserbotRefused, edit_problem_text

log = logging.getLogger(__name__)


class UserError(Exception):
    """An error whose message is safe and useful to show to the user."""


@dataclass
class Ctx:
    cfg: Config
    db: Database
    client: TelegramClient
    admins: set = field(default_factory=set)
    state: dict = field(default_factory=dict)  # user id -> {"mode": ..., ...}
    pending: dict = field(default_factory=dict)  # tokens for confirm dialogs (replace, repost)
    seen_channels: dict = field(default_factory=dict)  # channel id -> Channel entity seen in updates
    text_handlers: dict = field(default_factory=dict)  # mode -> async fn(event, state)
    callbacks: dict = field(default_factory=dict)  # prefix -> async fn(event, parts)
    pickers: dict = field(default_factory=dict)  # channel chooser kind -> async fn(event, arg, page) that draws it again
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one long job (replace / repost) at a time
    userbot: Any = None  # app.userbot.Userbot or None
    reporter: Any = None  # app.errorlog.ErrorReporter or None
    expirer: Any = None  # app.expiry.Expirer (timed deletions of broadcasts) or None
    syncer: Any = None  # app.syncer.Syncer (keeps My posts in step with the channels) or None
    sync_ignore: set = field(default_factory=set)  # (channel id, message id) of posts the owner made the bot forget


def is_cb(event) -> bool:
    return isinstance(event, events.CallbackQuery.Event) or getattr(event, "_is_callback", False)


def guard(ctx: Ctx, owner: bool = False) -> Callable:
    """Only allowed users may use a handler; errors are turned into friendly messages."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(event):
            uid = event.sender_id
            ok = ctx.cfg.is_owner(uid) if owner else (ctx.cfg.is_owner(uid) or uid in ctx.admins or uid in ctx.cfg.admins)
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
                log.error("Telegram refused a request in handler %s (user %s)", fn.__name__, uid, exc_info=True)
                await notify_error(event, "❌ " + explain_rpc(e))
            except Exception as e:
                log.exception("Unhandled exception in handler %s (user %s)", fn.__name__, uid)
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


# What the userbot can change in a post. It is a user account: it can't set buttons, and it fails the way the bot did
# when Telegram dislikes the text itself.
USERBOT_EDITS = {"text", "entities", "link_preview"}
NOT_THE_USERBOTS_JOB = {
    "EntityBoundsInvalidError", "EntitiesTooLongError", "MessageTooLongError", "MediaCaptionTooLongError",
    "MessageEmptyError", "ButtonUrlInvalidError", "ReplyMarkupInvalidError",
}


@dataclass
class EditReport:
    """How the edit of a published post went (for the message the owner gets)."""

    by_userbot: bool = False  # Telegram refused the bot, so the userbot made the edit
    bot_error: Optional[str] = None  # the name of the error Telegram gave the bot
    buttons: Optional[str] = None  # after the userbot's edit: "kept" | "restored" | "lost" (None: no link buttons saved)
    notes: list = field(default_factory=list)  # what the userbot had to do in the channel (joined, got a right ...)
    media: Optional[tuple] = None  # (kind, reference) of the photo / file the post shows now, as the bot sees it


def edit_report_text(report: Optional[EditReport]) -> str:
    """The lines to add to the owner's message when the userbot had to make the edit (empty for an ordinary edit)."""
    if report is None or not report.by_userbot:
        return ""
    lines = [f"🤖 Telegram refused the bot ({esc(report.bot_error or 'error')}), so the userbot made the edit."]
    if report.buttons == "kept":
        lines.append("The buttons are still on the post.")
    elif report.buttons == "restored":
        lines.append("Telegram dropped the buttons with that edit, so the bot put them back.")
    elif report.buttons == "lost":
        lines.append(
            "Telegram dropped the buttons with that edit and the bot may not put them back. They stay saved in "
            "My posts; 🔧 Check buttons tries again."
        )
    if report.notes:
        text = "; ".join(report.notes)
        lines.append("ℹ️ " + esc(text[:1].upper() + text[1:]) + ".")
    return "\n".join(lines)


async def put_buttons_back(client, ch, p) -> Optional[str]:
    """After the userbot's edit: make the post show its saved link buttons again where Telegram dropped them.

    "kept" (they were there), "restored", "lost" (Telegram won't let the bot set them), None (none are saved)."""
    markup = build_markup(p.buttons)
    if not link_urls(markup):
        return None
    try:
        how = await ensure_markup(client, peer_of(ch), p.message_id, markup)
    except Exception as e:  # noqa: BLE001 - the edit itself worked; only the buttons are missing
        log.info("could not put the buttons back on %s: %s", p.message_id, type(e).__name__)
        return "lost"
    return "kept" if how == "fine" else "restored"


async def live_media(client, ch, message_id: int) -> Optional[tuple]:
    """(kind, reference) of the photo / file the channel post shows now, as the bot sees it; None if it can't be read."""
    try:
        msgs = await fetch_messages(client, peer_of(ch), [message_id])
    except Exception:  # noqa: BLE001
        return None
    m = msgs[0] if msgs else None
    if is_skippable(m):
        return None
    kind, ref = media_info(m)
    return (kind, ref) if kind else None


async def edit_by_userbot(ctx: Ctx, ch, p, *, replace_media: bool, bot_error: str, notify=None) -> Optional[EditReport]:
    """The userbot makes the live post look like `p`. None when there is no userbot to ask; UserbotError when it fails.

    A new photo / file goes through the userbot's own chat with the bot: the bot sends it there and the userbot edits the
    post with that copy, so nothing is downloaded or uploaded - a big file is as quick as a small one and keeps its
    thumbnail. Only when that does not work does the bot download the file (with its thumbnail) and the userbot upload
    it again, which is slow for a big file. `notify(text)` tells the owner what is going on."""
    ub = ctx.userbot
    editor = await ub.editor_for(ch) if ub is not None else None
    if editor is None:
        return None
    report = EditReport(by_userbot=True, bot_error=bot_error)
    folder = None

    async def tell(text: str) -> None:
        if notify is not None:
            await notify(text)

    fields = dict(
        text=p.text or "", entities=de_entities(p.entities), preview=None if p.media_kind else bool(p.link_preview)
    )
    try:
        await tell("⏳ Telegram refused the bot, so the userbot is making the edit.")
        if not (replace_media and p.media_file_id):
            await editor.edit(p.message_id, **fields)
        else:
            done = False
            media = await editor.media_by_reference(p.media_file_id)
            if media is not None:
                try:
                    await editor.edit(p.message_id, media=media, **fields)
                    done = True
                    editor.notes.append("the new file went through the userbot's chat with the bot, so nothing was uploaded")
                except UserbotRefused as e:
                    if not e.about_media:
                        raise
                    editor.dm_problem = f"Telegram would not take the userbot's copy of the file ({e.name})"
            if not done:
                why = editor.dm_problem or "that did not work"
                editor.notes.append(f"the file was downloaded and uploaded again, because {why}")
                await tell(
                    "⏳ The file can't go through the userbot's chat with the bot, so it is downloaded and uploaded "
                    f"again - a big file takes a while. (Reason: {esc(why)}.)"
                )
                folder = tempfile.mkdtemp(prefix="cpb-media-")
                try:
                    media = await fetch_media(ctx.client, p.media_file_id, p.media_kind, folder)
                except (errors.RPCError, OSError, ValueError) as e:
                    raise UserbotError(f"I could not download the new media for the userbot ({type(e).__name__}).")
                await editor.edit(p.message_id, media=media, **fields)
    finally:
        await editor.close()
        report.notes = list(editor.notes)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    if replace_media:
        report.media = await live_media(ctx.client, ch, p.message_id)
    report.buttons = await put_buttons_back(ctx.client, ch, p)
    return report


async def apply_edit_reported(ctx: Ctx, post, ch, *, replace_media: bool = False, notify=None, **changes):
    """Edit the channel message first (if the post is published), and only then save the change.

    The bot edits. If Telegram refuses it (a post that belongs to another bot) and the change is to the text, caption
    or media, the userbot makes the edit instead. Returns (the saved post, an EditReport)."""
    report = EditReport()
    if post.status == "sent" and post.message_id:
        new = SimpleNamespace(**{**post.as_dict(), **changes})
        try:
            await push_edit(ctx.client, ch, new, replace_media=replace_media)
        except errors.MessageNotModifiedError:
            pass
        except errors.RPCError as e:
            refused = "❌ " + explain_rpc(e)
            if type(e).__name__ in NOT_THE_USERBOTS_JOB or not (replace_media or USERBOT_EDITS & set(changes)):
                raise UserError(refused + "\nNothing was changed.")
            try:
                done = await edit_by_userbot(
                    ctx, ch, new, replace_media=replace_media, bot_error=type(e).__name__, notify=notify
                )
            except UserbotError as ue:
                log.info("the userbot could not make the edit either: %s", ue)
                raise UserError(f"{refused} {edit_problem_text(ctx.userbot, fallback_error=str(ue))}\nNothing was changed.")
            if done is None:
                raise UserError(f"{refused} {edit_problem_text(ctx.userbot)}\nNothing was changed.")
            report = done
            if report.media:  # the post now shows the userbot's upload: keep My posts in step with it
                changes = {**changes, "media_kind": report.media[0], "media_file_id": report.media[1]}
    return await ctx.db.update_post(post.id, **changes), report


async def apply_edit(ctx: Ctx, post, ch, *, replace_media: bool = False, **changes):
    """apply_edit_reported for the callers that only want the saved post."""
    saved, _ = await apply_edit_reported(ctx, post, ch, replace_media=replace_media, **changes)
    return saved


def purge_pending(ctx: Ctx, ttl: float = 3600.0) -> None:
    """Forget confirm dialogs (replace plans, repost choices...) nobody answered for a while."""
    now = time.monotonic()
    stale = [
        k
        for k, v in ctx.pending.items()
        if not getattr(v, "running", False) and now - getattr(v, "created", now) > ttl
    ]
    for k in stale:
        ctx.pending.pop(k, None)


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
