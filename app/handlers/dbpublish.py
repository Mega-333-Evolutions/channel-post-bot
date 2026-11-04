"""Automatic publishing from the Database Channel: the listener for the autorename bot's messages in the communication chat,
and /autopublish - the message text and the button name of the posts this bot makes by itself (owner)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from telethon import Button, events

from ..common import Ctx, UserError, clear_state, cmd, guard, on_cb, on_text, require_owner, say, send_post, set_state, show
from ..dbpublish import (
    BUTTON_MAX,
    DEFAULT_BUTTON,
    DEFAULT_TEXT,
    TEXT_MAX,
    AutoPublisher,
    Templates,
    load_templates,
    marked_chat_id,
    render_post,
    reset_templates,
    save_templates,
    unknown_variables,
)
from ..tgutil import classify_media, esc, utf16_len
from ..ui import CANCEL
from .panel import read_text

log = logging.getLogger(__name__)

SAMPLE = {"episode": "05", "channel": "Your channel"}  # what the preview puts in
SAMPLE_LINK = "https://t.me/telegram"  # the preview's button opens this: the real one is the file link of the Share URL button

VARIABLES = (
    "<code>{episode}</code> - the episode number the autorename bot wrote in its message, as it wrote it (05 stays 05)\n"
    "<code>{channel}</code> - the name of the channel the post goes to"
)


def status_line(ctx: Ctx) -> str:
    cfg = ctx.cfg
    if cfg.auto_publish:
        return (
            "✅ <b>On.</b> When the autorename bot writes “File uploaded to the Database Channel” in the communication chat "
            f"(<code>{cfg.comm_chat}</code>), I post that episode in the channel its message names.\n"
            f"Database Channel: <code>{cfg.db_channel}</code>"
        )
    missing = " and ".join(n for n, v in (("DB_CHANNEL", cfg.db_channel), ("COMMUNICATION_CHAT", cfg.comm_chat)) if not v)
    return f"⏸ <b>Off.</b> Set {missing} to switch it on (both are needed)."


def panel(ctx: Ctx, t: Templates, note: str = "") -> tuple:
    lines = [
        "🤖 <b>Automatic publishing</b>",
        status_line(ctx),
        "",
        f"📝 <b>Message text</b> ({'your own' if t.custom_text else 'default'})",
        f"<pre>{esc(t.text)}</pre>",
        f"🔘 <b>Button name</b> ({'your own' if t.custom_button else 'default'})",
        f"<pre>{esc(t.button)}</pre>",
        "The button opens the file link of the 🔁 Share URL button of the Database Channel post.",
        "",
        "Variables:",
        VARIABLES,
    ]
    if t.entities:
        lines.insert(5, "<i>It has formatting (bold, links ...): 👁 Preview shows it.</i>")
    text = (f"{note}\n\n" if note else "") + "\n".join(lines)
    rows = [
        [Button.inline("✏️ Message text", "dpb:t"), Button.inline("✏️ Button name", "dpb:b")],
        [Button.inline("👁 Preview", "dpb:v")],
    ]
    resets = []
    if t.custom_text:
        resets.append(Button.inline("↩️ Default text", "dpb:rt"))
    if t.custom_button:
        resets.append(Button.inline("↩️ Default button", "dpb:rb"))
    if resets:
        rows.append(resets)
    rows.append([Button.inline("✖️ Close", "dpb:x")])
    return text, rows


def register(ctx: Ctx) -> None:
    client, db, cfg = ctx.client, ctx.db, ctx.cfg

    # ------------------------------------------------------------------------------ the communication chat
    if cfg.auto_publish:
        ctx.publisher = AutoPublisher(ctx)
        chat = marked_chat_id(cfg.comm_chat)

        @client.on(events.NewMessage(incoming=True, chats=chat, from_users=cfg.autorename_bot_id))
        async def h_notice(event):
            if getattr(event, "chat_id", None) != chat or getattr(event, "sender_id", None) != cfg.autorename_bot_id:
                return  # (the filters above do this already)
            await ctx.publisher.handle(event)

        log.info(
            "automatic publishing is on: Database Channel %s, communication chat %s, autorename bot %s",
            cfg.db_channel, cfg.comm_chat, cfg.autorename_bot_id,
        )
    elif cfg.db_channel or cfg.comm_chat:
        log.warning(
            "automatic publishing is off: it needs both DB_CHANNEL (%s) and COMMUNICATION_CHAT (%s)",
            cfg.db_channel or "missing", cfg.comm_chat or "missing",
        )

    # ------------------------------------------------------------------------------------- /autopublish
    async def show_panel(event, note: str = "", new: bool = False) -> None:
        text, kb = panel(ctx, await load_templates(db), note)
        if new:
            await say(event, text, kb)
        else:
            await show(event, text, kb)

    @client.on(cmd("autopublish"))
    @guard(ctx, owner=True)
    async def h_autopublish(event):
        clear_state(ctx, event.sender_id)
        await show_panel(event, new=True)

    @on_cb(ctx, "dpb")
    async def cb_panel(event, parts):
        require_owner(ctx, event)
        action, uid = parts[0], event.sender_id
        if action == "t":
            t = await load_templates(db)
            set_state(ctx, uid, "dpb_text")
            await show(
                event,
                "✏️ <b>Send the new message text.</b>\n"
                "Telegram formatting is kept (bold, links, spoilers ...); no Telegram editor at hand? Type HTML or Markdown (/formatting).\n\n"
                f"Variables:\n{VARIABLES}\n\n"
                f"Now:\n<pre>{esc(t.text)}</pre>\nDefault:\n<pre>{esc(DEFAULT_TEXT)}</pre>",
                CANCEL,
            )
        elif action == "b":
            t = await load_templates(db)
            set_state(ctx, uid, "dpb_button")
            await show(
                event,
                f"✏️ <b>Send the new button name</b> (one line, at most {BUTTON_MAX} characters).\n\n"
                f"Variables:\n{VARIABLES}\n\n"
                f"Now:\n<pre>{esc(t.button)}</pre>\nDefault:\n<pre>{esc(DEFAULT_BUTTON)}</pre>",
                CANCEL,
            )
        elif action == "v":
            t = await load_templates(db)
            text, entities, label = render_post(t, SAMPLE)
            shape = SimpleNamespace(
                text=text, entities=entities, buttons=[[{"t": label, "u": SAMPLE_LINK}]], media_file_id=None, link_preview=False
            )
            await send_post(client, await event.get_input_chat(), shape)
            await say(event, "👆 A post for episode 05 looks like this (its button opens a sample link here).")
        elif action == "rt":
            await reset_templates(db, text=True)
            await show_panel(event, "↩️ The message text is the default again.")
        elif action == "rb":
            await reset_templates(db, button=True)
            await show_panel(event, "↩️ The button name is the default again.")
        else:  # x
            clear_state(ctx, uid)
            await show(event, "Closed.")

    @on_text(ctx, "dpb_text")
    async def h_text(event, st):
        m = event.message
        if classify_media(m):
            raise UserError("Send the text only: the posts made by themselves are text posts with one button.")
        text, entities, how = read_text(m, cfg.markup_input, None, allow_rich=False)
        if not text.strip():
            raise UserError("Send some text.")
        if utf16_len(text) > TEXT_MAX:
            raise UserError(f"That is too long: at most {TEXT_MAX} characters (yours has {utf16_len(text)}).")
        bad = unknown_variables(text)
        if bad:
            raise UserError(f"{', '.join(bad)} is not a variable. Use {{episode}} or {{channel}}. Send the text again, or /cancel.")
        await save_templates(db, text=text, entities=entities)
        clear_state(ctx, event.sender_id)
        note = "✅ Message text saved." + (" Formatting read from HTML / Markdown." if how in ("html", "markdown") else "")
        await show_panel(event, note, new=True)

    @on_text(ctx, "dpb_button")
    async def h_button(event, st):
        raw = (event.raw_text or "").strip()
        if not raw:
            raise UserError("Send the button name as text.")
        if "\n" in raw:
            raise UserError("A button name is one line. Send it again, or /cancel.")
        if len(raw) > BUTTON_MAX:
            raise UserError(f"That is too long: at most {BUTTON_MAX} characters (yours has {len(raw)}).")
        bad = unknown_variables(raw)
        if bad:
            raise UserError(f"{', '.join(bad)} is not a variable. Use {{episode}} or {{channel}}. Send the name again, or /cancel.")
        await save_templates(db, button=raw)
        clear_state(ctx, event.sender_id)
        await show_panel(event, "✅ Button name saved.", new=True)
