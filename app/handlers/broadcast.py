"""/broadcast [time] (also spelled /boardcast): one message to every connected channel. Owner only.

With a time (50m, 1h, 5d) the message is deleted again from all the channels - and from My posts - when the time is up;
without one it stays.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

from telethon import Button, errors

from ..buttons import parse_buttons_text
from ..common import (
    Ctx,
    UserError,
    clear_state,
    cmd,
    discard_temp,
    edit_callback_message,
    guard,
    on_cb,
    on_text,
    purge_pending,
    require_owner,
    say,
    send_post,
    set_state,
    show,
)
from ..db import utcnow
from ..expiry import human, parse_duration, stamp
from ..tgutil import esc, explain_rpc, flood_retry, peer_of
from ..ui import CANCEL
from .panel import extract_content

log = logging.getLogger(__name__)

BUTTON_HELP = (
    "➕ <b>Buttons?</b> Send one row per line as <code>Button text - link</code> "
    "(several in one row: separate them with <code> | </code>), or skip."
)


@dataclass
class BroadcastDraft:
    """The message is made and previewed; the owner still has to confirm."""

    user: int
    content: dict  # text, entities, media_kind, media_file_id
    buttons: list
    ttl: Optional[int]  # seconds until it is deleted again; None = it stays
    created: float = field(default_factory=time.monotonic)


@dataclass
class BroadcastResult:
    group_id: str
    delete_at: Optional[datetime]
    ttl: Optional[int] = None  # seconds it stays; None = for good
    sent: list = field(default_factory=list)  # [(channel, message id)]
    failed: list = field(default_factory=list)  # [(channel, reason)]


def shape_of(draft: BroadcastDraft) -> SimpleNamespace:
    c = draft.content
    return SimpleNamespace(
        text=c.get("text") or "",
        entities=c.get("entities") or [],
        buttons=draft.buttons,
        media_kind=c.get("media_kind"),
        media_file_id=c.get("media_file_id"),
        link_preview=False,
    )


async def send_broadcast(ctx: Ctx, chans: list, draft: BroadcastDraft, *, delay: float = 1.0, progress=None) -> BroadcastResult:
    """Post the message in every channel, save each copy in My posts and, if it expires, schedule its deletion."""
    db = ctx.db
    delete_at = utcnow() + timedelta(seconds=draft.ttl) if draft.ttl else None
    res = BroadcastResult(group_id=secrets.token_hex(6), delete_at=delete_at, ttl=draft.ttl)
    shape = shape_of(draft)
    for n, ch in enumerate(chans, 1):
        try:
            sent = await flood_retry(lambda: send_post(ctx.client, peer_of(ch), shape))
        except errors.RPCError as e:
            res.failed.append((ch, explain_rpc(e)))
            continue
        except Exception as e:
            log.exception("broadcast: posting in channel %s failed", ch.id)
            res.failed.append((ch, type(e).__name__))
            continue
        await db.create_post(
            channel_id=ch.id,
            message_id=sent.id,
            status="sent",
            source="bot",
            created_by=draft.user,
            text=shape.text,
            entities=shape.entities,
            media_kind=shape.media_kind,
            media_file_id=shape.media_file_id,
            buttons=shape.buttons,
            link_preview=False,
            sent_at=utcnow(),
        )
        res.sent.append((ch, sent.id))
        if delete_at is not None:  # saved at once, so a restart in the middle loses nothing
            await db.schedule_deletes(
                [dict(channel_id=ch.id, message_id=sent.id, delete_at=delete_at, group_id=res.group_id, created_by=draft.user)]
            )
            if ctx.expirer is not None:
                ctx.expirer.notify(delete_at)
        if progress:
            await progress(n)
        if n < len(chans):
            await asyncio.sleep(delay)
    return res


def report(res: BroadcastResult, total: int) -> str:
    lines = [f"📣 <b>Broadcast sent</b> to {len(res.sent)} of {total} channel(s)"]
    lines += [f"✅ {esc(ch.title)}" for ch, _ in res.sent]
    lines += [f"❌ {esc(ch.title)} - {esc(why)}" for ch, why in res.failed]
    if res.delete_at is not None and res.sent:
        lines += ["", f"🕒 It is deleted from these channels and from My posts in {human(res.ttl)} ({stamp(res.delete_at)})."]
    elif res.sent:
        lines += ["", "📌 No time was given, so it stays in the channels."]
    return "\n".join(lines)


def register(ctx: Ctx) -> None:
    client, db, cfg = ctx.client, ctx.db, ctx.cfg

    @client.on(cmd("broadcast", args=True))
    @client.on(cmd("boardcast", args=True))
    @guard(ctx, owner=True)
    async def h_broadcast(event):
        purge_pending(ctx)
        raw = (event.pattern_match.group(1) or "").strip()
        ttl = None
        if raw:
            try:
                ttl = parse_duration(raw)
            except ValueError as e:
                raise UserError(str(e))
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels yet. Register one with /addchannel first.")
        uid = event.sender_id
        await discard_temp(ctx, uid)
        set_state(ctx, uid, "bc_content", ttl=ttl)
        when = (
            f"⏳ It is deleted from all channels - and from My posts - {human(ttl)} after it is sent."
            if ttl
            else "📌 No time given: it stays in the channels for good. (For a timed one use e.g. <code>/broadcast 1h</code>.)"
        )
        await say(
            event,
            f"📣 <b>Broadcast</b> to {len(chans)} channel(s)\n{when}\n\n"
            "✍️ <b>Send the message now.</b> Text, or a photo / video / GIF / file with a caption. Formatting is kept.",
            CANCEL,
        )

    @on_text(ctx, "bc_content")
    async def h_content(event, st):
        m = event.message
        if m.grouped_id and st.get("group") == m.grouped_id:
            return  # the other items of an album: only the first one is used
        if m.grouped_id:
            st["group"] = m.grouped_id
        content = extract_content(m)
        set_state(ctx, event.sender_id, "bc_buttons", ttl=st["ttl"], content=content)
        await say(event, BUTTON_HELP, [[Button.inline("⏭ No buttons", "brs")], CANCEL[0]])

    async def confirm_step(event, st, rows: list) -> None:
        uid = event.sender_id
        draft = BroadcastDraft(user=uid, content=st["content"], buttons=rows, ttl=st["ttl"])
        chans = await db.list_channels()
        if not chans:
            clear_state(ctx, uid)
            raise UserError("No channels any more. Register one with /addchannel first.")
        try:
            await send_post(client, await event.get_input_chat(), shape_of(draft))  # the preview
        except errors.RPCError as e:
            raise UserError("❌ " + explain_rpc(e) + "\nThe message can't be sent like this - send it again.")
        token = secrets.token_hex(4)
        ctx.pending[token] = draft
        clear_state(ctx, uid)
        names = "\n".join(f"• {esc(c.title)}" for c in chans[:15]) + (f"\n• … and {len(chans) - 15} more" if len(chans) > 15 else "")
        when = (
            f"⏳ Deleted again {human(draft.ttl)} after sending."
            if draft.ttl
            else "📌 Permanent (no time given)."
        )
        await say(
            event,
            f"👆 That is the message. Send it to <b>{len(chans)}</b> channel(s)?\n{names}\n\n{when}",
            [[Button.inline("📣 Send to all channels", f"brc:{token}")], [Button.inline("✖️ Cancel", f"brn:{token}")]],
        )

    @on_text(ctx, "bc_buttons")
    async def h_buttons(event, st):
        if event.message.grouped_id:
            return
        try:
            rows = parse_buttons_text(event.raw_text or "")
        except ValueError as e:
            raise UserError(f"❌ {e}\n\nTry again, or tap “No buttons”.")
        if not rows:
            raise UserError("Send at least one button as “Text - link”, or tap “No buttons”.")
        await confirm_step(event, st, rows)

    @on_cb(ctx, "brs")
    async def cb_no_buttons(event, parts):
        require_owner(ctx, event)
        st = ctx.state.get(event.sender_id)
        if not st or st.get("mode") != "bc_buttons":
            raise UserError("That step is no longer active. Start again with /broadcast.")
        await confirm_step(event, st, [])

    @on_cb(ctx, "brn")
    async def cb_cancel(event, parts):
        require_owner(ctx, event)
        ctx.pending.pop(parts[0], None)
        await show(event, "Cancelled - nothing was sent.")

    @on_cb(ctx, "brc")
    async def cb_send(event, parts):
        require_owner(ctx, event)
        token = parts[0]
        draft = ctx.pending.get(token)
        if not isinstance(draft, BroadcastDraft) or draft.user != event.sender_id:
            raise UserError("That broadcast has expired. Start again with /broadcast.")
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels any more. Register one with /addchannel first.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace / repost / shift) is still running. Try again in a minute.")
        ctx.pending.pop(token, None)  # a second press can't send it twice
        last = [0.0]

        async def prog(n):
            if time.monotonic() - last[0] < 3:
                return
            last[0] = time.monotonic()
            try:
                await edit_callback_message(event, f"📣 Sending… {n} of {len(chans)} channel(s)")
            except Exception:
                pass

        async with ctx.lock:
            res = await send_broadcast(ctx, chans, draft, delay=cfg.edit_delay, progress=prog)
        await edit_callback_message(event, report(res, len(chans)))
