"""/start /help /cancel /export /userbot /testerror, plus the routers for free text and inline-button presses."""
from __future__ import annotations

import asyncio
import io
import json
import logging

from telethon import events

from ..common import Ctx, cmd, clear_state, discard_temp, guard, say, show
from ..tgutil import esc

log = logging.getLogger(__name__)

HELP = (
    "<b>Channel Post Bot</b>\n\n"
    "<b>Posts</b>\n"
    "/new - create a post (text or media, with buttons)\n"
    "/posts - your posts and drafts: edit text, caption, media and buttons, or delete them\n"
    "/cancel - stop what you're doing\n\n"
    "<b>Channels</b>\n"
    "/addchannel - register a channel (the bot must be an admin there)\n"
    "/channels - list or remove channels\n\n"
    "<b>Owner tools</b>\n"
    "/replace @old @new - swap the username inside t.me links (button links and hyperlinks) in all channels\n"
    "/undo - undo the last /replace\n"
    "/repost @old @new - copy every post of a channel in order (links swapped), then delete the old posts\n"
    "/userbot - status of the helper account that deletes posts the bot is not allowed to delete\n"
    "/testerror - send a test error to the error log chat\n"
    "/export - download a JSON backup of your posts"
)


def register(ctx: Ctx) -> None:
    client, db = ctx.client, ctx.db

    @client.on(cmd("start"))
    @client.on(cmd("help"))
    @guard(ctx)
    async def h_help(event):
        await say(event, HELP)

    @client.on(cmd("cancel"))
    @guard(ctx)
    async def h_cancel(event):
        await discard_temp(ctx, event.sender_id)
        clear_state(ctx, event.sender_id)
        await say(event, "Cancelled.")

    @client.on(cmd("export"))
    @guard(ctx, owner=True)
    async def h_export(event):
        data = await db.export_all()
        buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
        buf.name = "channel-posts-export.json"
        await event.respond(f"📦 Backup: {len(data['channels'])} channel(s), {len(data['posts'])} post(s).", file=buf)

    @client.on(cmd("userbot"))
    @guard(ctx, owner=True)
    async def h_userbot(event):
        ub = ctx.userbot
        if ub is None or not ub.enabled:
            await say(
                event,
                "🤖 <b>Userbot: not set up.</b>\n"
                "The bot deletes old posts itself first. If Telegram refuses (posts older than 48 hours), a helper "
                "account - the userbot - can do it: it joins the channel through an invite link the bot makes, the bot "
                "gives it the “Delete messages” right if it has the “Add new admins” right, and takes it away again "
                "afterwards.\n\n"
                "To set it up: on your own computer run <code>python make_userbot_session.py</code> with a "
                "<b>separate</b> Telegram account, and put the printed text into the bot's secrets as "
                "<code>USERBOT_SESSION</code>. See the README.",
            )
            return
        if not ub.ready:
            await ub.connect()
        kept = "stays an admin" if ub.keep_admin else "is made admin only while deleting"
        await say(event, f"🤖 <b>Userbot:</b> {esc(ub.describe())}\nIt {kept} (USERBOT_KEEP_ADMIN).")

    @client.on(cmd("testerror", args=True))
    @guard(ctx, owner=True)
    async def h_testerror(event):
        rep = getattr(ctx, "reporter", None)
        if rep is None or not rep.enabled:
            await say(event, "The error log is switched off (ERROR_LOG_CHAT_ID is 0 / off).")
            return
        mode = (event.pattern_match.group(1) or "").strip().lower()
        if mode == "task":
            async def boom():
                raise RuntimeError("Test error from /testerror task")

            asyncio.get_running_loop().create_task(boom())  # nobody awaits it, like a forgotten background task
            await say(event, "A background task will fail in a moment - look in the error log chat.")
            return
        await say(event, "Raising a test error now - look in the error log chat. (Use <code>/testerror task</code> to test a background task.)")
        try:
            raise KeyError("test")
        except KeyError as e:
            raise RuntimeError("Test error from /testerror") from e

    @client.on(
        events.NewMessage(incoming=True, func=lambda e: e.is_private and not (e.raw_text or "").startswith("/"))
    )
    @guard(ctx)
    async def h_free_text(event):
        st = ctx.state.get(event.sender_id)
        if not st:
            await say(event, "Use /new to create a post or /posts to manage your posts. /help lists everything.")
            return
        fn = ctx.text_handlers.get(st["mode"])
        if fn is None:
            clear_state(ctx, event.sender_id)
            await say(event, "That step is no longer active. Start again with /new.")
            return
        await fn(event, st)

    @client.on(events.CallbackQuery())
    @guard(ctx)
    async def h_callback(event):
        try:
            await event.answer()  # stop the loading spinner right away
        except Exception:
            pass
        data = event.data.decode("utf-8", "replace")
        prefix, *parts = data.split(":")
        fn = ctx.callbacks.get(prefix)
        if fn is None:
            await event.respond("This button is no longer active. Use /new or /posts.")
            return
        await fn(event, parts)

    async def cb_noop(event, parts):
        await event.answer()

    async def cb_cancel(event, parts):
        uid = event.sender_id
        await discard_temp(ctx, uid)
        clear_state(ctx, uid)
        await show(event, "Cancelled.")

    ctx.callbacks["noop"] = cb_noop
    ctx.callbacks["cx"] = cb_cancel
