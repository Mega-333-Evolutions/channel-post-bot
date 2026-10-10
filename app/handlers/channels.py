"""Channel registration: /addchannel, /channels."""
from __future__ import annotations

import logging
import re

from telethon import Button, events, types

from .. import picker
from ..channelref import parse_channel_ref
from ..common import Ctx, UserError, clear_state, cmd, guard, on_cb, on_text, require_owner, say, set_state, show
from ..tgutil import esc, get_rights
from ..ui import CANCEL

log = logging.getLogger(__name__)

ADD_HELP = (
    "📢 <b>Add a channel</b>\n"
    "1. Make the bot an admin of the channel with the rights <i>Post messages</i>, <i>Edit messages of others</i> "
    "and <i>Delete messages</i>.\n"
    "2. Then <b>forward any post from that channel to me</b>, or send its @username (public channels).\n\n"
    "Tip for private channels: if forwarding doesn't work, post something in the channel while the bot is "
    "running, then forward that post."
)
NEEDS_ACCESS = (
    "I can see the channel, but Telegram hasn't given me its access details yet. Post something in the "
    "channel while the bot is running (or send the @username if it's public), then forward a post again."
)


def rights_line(r) -> str:
    if r is None:
        return "rights unknown"
    if not r.admin:
        return "⚠️ not an admin there"
    ok = lambda v: "✅" if v else "❌"  # noqa: E731
    return f"post {ok(r.post)} · edit others' posts {ok(r.edit)} · delete {ok(r.delete)}"


def register(ctx: Ctx) -> None:
    client, db = ctx.client, ctx.db

    # remember channels whose full details (access hash) we have seen in updates
    @client.on(events.NewMessage(func=lambda e: bool(e.is_channel) and not e.is_group))
    async def watch_channels(event):
        try:
            chat = await event.get_chat()
            if isinstance(chat, types.Channel) and chat.access_hash and not getattr(chat, "min", False):
                prev = ctx.seen_channels.get(chat.id)
                ctx.seen_channels[chat.id] = chat
                if prev is None or prev.access_hash != chat.access_hash:
                    await db.touch_channel_hash(chat.id, chat.access_hash)
        except Exception:  # pragma: no cover
            log.debug("channel watcher failed", exc_info=True)

    @client.on(cmd("addchannel"))
    @guard(ctx, owner=True)
    async def h_addchannel(event):
        set_state(ctx, event.sender_id, "add_channel")
        await say(event, ADD_HELP, CANCEL)

    @on_text(ctx, "add_channel")
    async def h_add_channel(event, st):
        m = event.message
        ent = None
        fwd = m.forward
        if fwd is not None and isinstance(getattr(fwd, "chat", None), types.Channel):
            ent = fwd.chat
            if getattr(ent, "min", False) or not getattr(ent, "access_hash", None):
                ent = ctx.seen_channels.get(ent.id, ent)
        else:
            ref = parse_channel_ref(m.raw_text or "")
            if ref is None:
                raise UserError("Forward a post from the channel to me, or send its @username.")
            if isinstance(ref, int):
                ent = ctx.seen_channels.get(ref)
            if ent is None:
                try:
                    ent = await client.get_entity(ref if isinstance(ref, str) else types.PeerChannel(ref))
                except Exception:
                    ent = None
        if not isinstance(ent, types.Channel):
            raise UserError("I couldn't find that channel.\n\n" + re.sub(r"<[^>]+>", "", ADD_HELP))
        if not ent.broadcast:
            raise UserError("That is a group, not a channel. Only channels are supported.")
        if not ent.access_hash or getattr(ent, "min", False):
            raise UserError(NEEDS_ACCESS)
        rights = await get_rights(client, ent)
        if rights is not None and not rights.admin:
            raise UserError("The bot is not an admin of that channel yet. Add it as an admin, then try again.")
        await db.save_channel(ent.id, ent.access_hash, ent.title or "", ent.username, event.sender_id)
        clear_state(ctx, event.sender_id)
        warn = ""
        if rights is not None and not (rights.post and rights.edit and rights.delete):
            warn = "\n\n⚠️ Missing rights: give the bot <i>Post</i>, <i>Edit messages of others</i> and <i>Delete</i> so everything works."
        await say(
            event,
            f"✅ Added <b>{esc(ent.title or '')}</b>\n{rights_line(rights)}{warn}\n\nCreate a post with /new.",
        )

    async def show_channels(event, arg: str = "", page: int = 0) -> None:
        """The list of channels; the owner's number buttons choose one to remove."""
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels yet. Use /addchannel.")
        owner = ctx.cfg.is_owner(event.sender_id)
        text, kb = picker.render(
            chans,
            page,
            head="📢 <b>Your channels</b>",
            kind="ch",
            choose=(lambda c: f"chd:{c.id}") if owner else None,
            with_username=True,
            hint="To take a channel out of the bot, tap its number.",
        )
        await show(event, text, kb or None)

    ctx.pickers["ch"] = show_channels

    @client.on(cmd("channels"))
    @guard(ctx)
    async def h_channels(event):
        await show_channels(event)

    @on_cb(ctx, "chd")
    async def cb_remove_channel(event, parts):
        """A number was tapped: ask before the channel is taken out of the bot."""
        require_owner(ctx, event)
        ch = await db.get_channel(int(parts[0]))
        if ch is None or not ch.active:
            await show_channels(event)
            return
        kb = [[Button.inline("✅ Yes, remove it", f"chy:{ch.id}"), Button.inline("🔙 Back", f"{picker.PAGE_CB}:ch::0")]]
        await show(
            event,
            f"➖ Take <b>{esc(ch.title)}</b> out of the bot?\n"
            "Its saved posts are kept, and you can add it again with /addchannel.",
            kb,
        )

    @on_cb(ctx, "chy")
    async def cb_remove_channel_yes(event, parts):
        require_owner(ctx, event)
        await db.set_channel_active(int(parts[0]), False)
        if ctx.syncer is not None:
            ctx.syncer.forget_cache()
        await show(event, "Channel removed from the bot. Its saved posts are kept; add it again with /addchannel.")
