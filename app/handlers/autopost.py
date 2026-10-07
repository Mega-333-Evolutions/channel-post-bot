"""/autopost <total> <interval>: post "Episodes 01 to 20", "Episodes 21 to 40", ... with a download button each,
then ask for the real link of every button. Owners and admins."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from telethon import Button, errors

from ..autopost import (
    MAX_POSTS,
    PLACEHOLDER_LINK,
    USAGE,
    button_label,
    parse_autopost_args,
    plan_ranges,
    post_buttons,
    post_text,
    ranges_summary,
    title_of,
)
from ..common import (
    Ctx,
    UserError,
    clear_state,
    cmd,
    discard_temp,
    edit_callback_message,
    guard,
    on_cb,
    purge_pending,
    say,
    send_post,
)
from ..db import utcnow
from ..tgutil import esc, explain_rpc, flood_retry, get_rights, peer_of, short
from .masslinks import begin_queue

log = logging.getLogger(__name__)


@dataclass
class AutoChoice:
    """/autopost was typed with good numbers; the channel still has to be picked."""

    user: int
    total: int
    interval: int
    ranges: list
    created: float = field(default_factory=time.monotonic)


@dataclass
class AutoResult:
    posts: list = field(default_factory=list)  # the saved Post rows, in the order they were posted
    failure: str = ""  # why it stopped before the end (empty = all posted)


async def post_series(ctx: Ctx, ch, ranges: list, user_id: int, *, delay: float = 1.0, progress=None) -> AutoResult:
    """Send one post per range to `ch` and save each in My posts. Stops at the first post Telegram refuses."""
    res = AutoResult()
    for n, (a, b) in enumerate(ranges, 1):
        text, buttons = post_text(a, b), post_buttons(a, b)
        shape = SimpleNamespace(text=text, entities=[], buttons=buttons, media_file_id=None, link_preview=False)
        try:
            sent = await flood_retry(lambda: send_post(ctx.client, peer_of(ch), shape))
        except errors.RPCError as e:
            res.failure = f"{title_of(a, b)}: {explain_rpc(e)}"
            break
        except Exception as e:
            log.exception("autopost: posting %s failed", title_of(a, b))
            res.failure = f"{title_of(a, b)}: {type(e).__name__}"
            break
        res.posts.append(
            await ctx.db.create_post(
                channel_id=ch.id,
                message_id=sent.id,
                status="sent",
                source="bot",
                created_by=user_id,
                text=text,
                entities=[],
                buttons=buttons,
                link_preview=False,
                sent_at=utcnow(),
            )
        )
        if progress:
            await progress(n)
        if n < len(ranges):
            await asyncio.sleep(delay)
    return res


def register(ctx: Ctx) -> None:
    client, db, cfg = ctx.client, ctx.db, ctx.cfg

    @client.on(cmd("autopost", args=True))
    @guard(ctx)
    async def h_autopost(event):
        purge_pending(ctx)
        raw = (event.pattern_match.group(1) or "").strip()
        if not raw:
            await say(event, USAGE)
            return
        try:
            total, interval = parse_autopost_args(raw)
        except ValueError as e:
            raise UserError(f"{e}\n\n" + re.sub(r"<[^>]+>", "", USAGE).replace("&lt;", "<").replace("&gt;", ">"))
        ranges = plan_ranges(total, interval)
        if len(ranges) > MAX_POSTS:
            raise UserError(f"That would be {len(ranges)} posts - at most {MAX_POSTS} at once. Use a bigger interval.")
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels yet. Register one with /addchannel first.")
        uid = event.sender_id
        await discard_temp(ctx, uid)
        clear_state(ctx, uid)
        token = secrets.token_hex(4)
        ctx.pending[token] = AutoChoice(user=uid, total=total, interval=interval, ranges=ranges)
        first = ranges[0]
        kb = [[Button.inline(f"📢 {short(c.title, 40)}", f"apc:{token}:{c.id}")] for c in chans]
        kb.append([Button.inline("✖️ Cancel", f"apx:{token}")])
        await say(
            event,
            f"📋 <b>Autopost</b>: {len(ranges)} post(s) for {total} episodes (every {interval})\n"
            f"{ranges_summary(ranges)}\n\n"
            f"Each post says “{esc(title_of(*first))}” with the button “{esc(button_label(*first))}”. The buttons get "
            f"the link {esc(PLACEHOLDER_LINK)} for now; when everything is posted I ask you for the real link of "
            "each button, one after the other.\n\n"
            "📢 <b>Which channel should I post them in?</b>",
            kb,
        )

    @on_cb(ctx, "apc")
    async def cb_pick(event, parts):
        token, cid = parts[0], int(parts[1])
        choice = ctx.pending.get(token)
        if not isinstance(choice, AutoChoice) or choice.user != event.sender_id:
            raise UserError("That list has expired. Run /autopost again.")
        ch = await db.get_channel(cid)
        if ch is None or not ch.active:
            raise UserError("That channel is not registered any more. Run /autopost again.")
        rights = await get_rights(client, ch)
        if rights is not None and not (rights.admin and rights.post):
            raise UserError("The bot needs the “Post messages” admin right in that channel.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace / repost / shift) is still running. Try again in a minute.")
        ctx.pending.pop(token, None)  # a second press on the list can't post everything twice
        last = [0.0]

        async def prog(n):
            if time.monotonic() - last[0] < 3:
                return
            last[0] = time.monotonic()
            try:
                await edit_callback_message(event, f"📤 Posting in <b>{esc(ch.title)}</b>: {n} of {len(choice.ranges)}…")
            except Exception:
                pass

        async with ctx.lock:
            res = await post_series(ctx, ch, choice.ranges, event.sender_id, delay=cfg.edit_delay, progress=prog)
        if not res.posts:
            raise UserError(f"Nothing was posted. {res.failure}")
        done = [(p.id, 0, 0) for p in res.posts]
        lines = [
            f"✅ Posted {len(res.posts)} of {len(choice.ranges)} in <b>{esc(ch.title)}</b>",
            ranges_summary(choice.ranges[: len(res.posts)]),
            f"Their buttons open {esc(PLACEHOLDER_LINK)} until you send the real links.",
        ]
        if res.failure:
            lines.append(f"\n⚠️ Stopped early - {esc(res.failure)}\nThe rest was not posted; run /autopost again for it.")
        await edit_callback_message(event, "\n".join(lines))
        await begin_queue(ctx, event, kind="auto", cid=ch.id, items=done, new=True)

    @on_cb(ctx, "apx")
    async def cb_cancel(event, parts):
        ctx.pending.pop(parts[0], None)
        await edit_callback_message(event, "Cancelled - nothing was posted.")
