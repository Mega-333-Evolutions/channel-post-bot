"""/start /help /cancel /export, plus the routers for free text and inline-button presses."""
from __future__ import annotations

import io
import json
import logging

from telethon import events

from ..common import Ctx, cmd, clear_state, discard_temp, guard, say, show

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
    "/testedit &lt;post link&gt; - check that the bot can edit the buttons of one specific post\n"
    "/selftest - check that editing buttons works on a post made by this bot\n"
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
