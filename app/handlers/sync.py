"""/sync (owner only) and the live watch over the channels.

The live part listens to Telegram's updates: a post deleted, edited or added in a connected channel is recorded in My
posts a few seconds later, and service messages ("pinned a message", "channel photo changed" ...) are deleted at once.
The same comparison runs on a timer for everything the bot was not there to see (SYNC_INTERVAL_MINUTES).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from telethon import events, types

from ..common import Ctx, UserError, cmd, guard, say
from ..sync_engine import SyncReport
from ..syncer import Syncer
from ..tgutil import esc
from .replace import pick_channels

log = logging.getLogger(__name__)

USAGE = (
    "Usage: <code>/sync</code>\n"
    "Compares every channel with My posts right now: posts that were deleted or edited in the channel by someone else "
    "are updated here, new posts that were not made with this bot are added, and service messages (“pinned a "
    "message”, “channel photo changed” ...) that appeared meanwhile are deleted.\n"
    "Options: <code>--channel @name</code> (only that channel), <code>--clean</code> (also delete the service messages "
    "that are already in the channel's history; the helper account, if you set one up, deletes the old ones the bot may not)."
)


def parse_sync_args(raw: str) -> tuple:
    """(channel reference or None, clean) - ValueError with a message for anything else."""
    toks = (raw or "").split()
    ref: Optional[str] = None
    clean = False
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--channel" and i + 1 < len(toks):
            ref = toks[i + 1]
            i += 2
        elif t == "--clean":
            clean = True
            i += 1
        else:
            raise ValueError(f"I don't know “{t}”.")
    return ref, clean


def ids_text(ids, limit: int = 8) -> str:
    ids = sorted(ids)
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown + (f" and {len(ids) - limit} more" if len(ids) > limit else "")


def channel_block(rep: SyncReport) -> str:
    lines = [f"<b>{esc(rep.channel.title)}</b>"]
    if rep.error:
        return "\n".join(lines + [f"⚠️ {esc(rep.error)}"])
    nothing = True
    if rep.first_look:
        lines.append(
            "👀 First look at this channel. From now on, posts made, edited or deleted outside this bot are recorded "
            "here (posts from before are not imported)."
        )
    if rep.checked:
        lines.append(f"✓ {rep.checked} saved post(s) compared with the channel")
    if rep.deleted:
        nothing = False
        lines.append(f"🗑 {len(rep.deleted)} deleted in the channel → removed from My posts (message {ids_text(rep.deleted)})")
    if rep.edited:
        nothing = False
        lines.append(f"✏️ {len(rep.edited)} edited in the channel → updated in My posts (message {ids_text(rep.edited)})")
    if rep.adopted:
        nothing = False
        lines.append(f"➕ {len(rep.adopted)} new post(s) made outside the bot → added to My posts (message {ids_text(rep.adopted)})")
    if rep.refreshed:
        lines.append(f"• {rep.refreshed} saved post(s) tidied up (spaces at the ends of the text)")
    if rep.services_deleted:
        nothing = False
        lines.append(f"🧹 {rep.services_deleted} service message(s) deleted")
    if rep.services_failed:
        nothing = False
        lines.append(
            f"⚠️ {rep.services_failed} service message(s) could not be deleted ({esc(rep.services_error or 'refused')}) - "
            "the bot needs the “Delete messages” right, and Telegram lets bots delete only recent messages"
        )
    if rep.buttons_missing:
        nothing = False
        lines.append(
            f"⚠️ {len(rep.buttons_missing)} saved post(s) show no buttons in the channel (message {ids_text(rep.buttons_missing)}). "
            "The saved buttons were kept: /posts → the channel → 🔧 Check buttons puts them back"
        )
    if rep.unsupported:
        lines.append(f"• {rep.unsupported} new message(s) of a kind My posts can't hold (poll, sticker ...) were left alone")
    fresh = len(rep.young) + len(rep.young_new)
    if fresh:
        lines.append(f"⏳ {fresh} too fresh to judge - the next check will see them")
    if rep.changed_meanwhile:
        lines.append(f"• {rep.changed_meanwhile} saved post(s) were changed through the bot meanwhile - left as they are")
    if rep.anomaly:
        nothing = False
        lines.append(f"⚠️ {esc(rep.anomaly)}")
    if rep.busy:
        lines.append("⏸ A long job started, so this check stopped early.")
    if nothing and not rep.busy and not rep.first_look:
        lines.append("Everything matches.")
    return "\n".join(lines)


def report_text(reports: list, *, clean: bool = False) -> str:
    head = "🔄 <b>Sync finished</b>" + (" (with the service-message clean-up)" if clean else "")
    if not reports:
        return head + "\nNothing to look at."
    return head + "\n\n" + "\n\n".join(channel_block(r) for r in reports)


def channel_id_of(msg) -> Optional[int]:
    peer = getattr(msg, "peer_id", None)
    return peer.channel_id if isinstance(peer, types.PeerChannel) else None


def register(ctx: Ctx) -> None:
    client, db = ctx.client, ctx.db
    if ctx.syncer is None:
        ctx.syncer = Syncer(ctx)
    sy = ctx.syncer

    # ----------------------------------------------------------------------------------------- /sync
    @client.on(cmd("sync", args=True))
    @guard(ctx, owner=True)
    async def h_sync(event):
        raw = (event.pattern_match.group(1) or "").strip()
        try:
            ref, clean = parse_sync_args(raw)
            chans = pick_channels(await db.list_channels(), ref)
        except ValueError as e:
            raise UserError(f"{e}\n\n" + re.sub(r"<[^>]+>", "", USAGE))
        if not chans:
            raise UserError("No channels registered yet. Use /addchannel.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace, repost, shift ...) is still running - try again when it has finished.")
        async with ctx.lock:
            status = await say(event, f"🔄 Looking at {len(chans)} channel(s)…")
            last = [0.0]

            async def prog(ch, rep) -> None:
                now = time.monotonic()
                if now - last[0] < 3:
                    return
                last[0] = now
                try:
                    await status.edit(f"🔄 <b>{esc(ch.title)}</b>: {rep.phase} (message {rep.scanned_to})…")
                except Exception:
                    pass

            reports = await sy.run_all(chans, explicit=True, clean=clean, progress=prog)
        await say(event, report_text(reports, clean=clean))

    # ------------------------------------------------------------------- what Telegram tells while we run
    @client.on(events.Raw(types.UpdateNewChannelMessage))
    async def on_channel_message(update):
        try:
            m = update.message
            cid = channel_id_of(m)
            if cid is None:
                return
            if isinstance(m, types.MessageService):
                await sy.on_service(cid, m)
            elif not isinstance(m, types.MessageEmpty):
                sy.note_new(cid, m)
        except Exception:
            log.warning("could not handle a new channel message", exc_info=True)

    @client.on(events.Raw(types.UpdateEditChannelMessage))
    async def on_channel_edit(update):
        try:
            m = update.message
            cid = channel_id_of(m)
            if cid is not None and not isinstance(m, (types.MessageService, types.MessageEmpty)):
                sy.note_edit(cid, m)
        except Exception:
            log.warning("could not handle an edited channel message", exc_info=True)

    @client.on(events.Raw(types.UpdateDeleteChannelMessages))
    async def on_channel_delete(update):
        try:
            await sy.note_deleted(update.channel_id, list(update.messages))
        except Exception:
            log.warning("could not handle deleted channel messages", exc_info=True)
