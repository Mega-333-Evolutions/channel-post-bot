"""/shift <source> <destination> [from_id] [to_id]: copy posts from one channel into another. Owner only."""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from telethon import Button, errors

from ..channelref import ChannelRef, resolve_channel
from ..common import Ctx, UserError, cmd, edit_callback_message, guard, on_cb, purge_pending, require_owner, say
from ..repost_engine import RepostOptions, RepostPlan, plan_repost
from ..shift_engine import delete_shift_copies, run_shift
from ..tgutil import esc, explain_rpc, get_rights, post_link
from ..userbot import delete_problem_text
from .repost import finishing_notes

log = logging.getLogger(__name__)

USAGE = (
    "Usage: <code>/shift &lt;source&gt; &lt;destination&gt; [from_id] [to_id]</code>\n"
    "Copies posts from one channel into another - text, formatting, media, albums, buttons, replies - and registers "
    "the copies in My posts. The source is only read: nothing in it is changed or deleted.\n\n"
    "A channel can be given as <code>@username</code>, a t.me link, its id (<code>-100…</code>) or an invite link "
    "(the bot must be a member). The bot must be an admin of the destination (Post messages).\n\n"
    "<code>/shift @source @destination</code> - every post\n"
    "<code>/shift @source @destination 15 20</code> - only the posts with message ids 15 to 20\n"
    "<code>/shift @source @destination 15</code> - only post 15"
)


@dataclass
class ShiftJob:
    """A shift that was planned and is waiting for the owner's OK (or is running)."""

    user: int
    src: ChannelRef
    dst: ChannelRef
    plan: RepostPlan
    already: int = 0  # posts of the range that an earlier shift copied into this destination
    running: bool = False
    stop: bool = False
    shift_id: str = ""
    created: float = field(default_factory=time.monotonic)


def parse_shift_args(raw: str) -> tuple:
    """-> (source text, destination text, first id or None, last id or None)"""
    toks = (raw or "").split()
    if len(toks) < 2 or len(toks) > 4:
        raise ValueError("Give the source and the destination channel, and optionally the first and the last message id.")
    nums: list = []
    for t in toks[2:]:
        if not t.isdigit() or int(t) < 1:
            raise ValueError(f"“{t}” is not a message id. Message ids are positive numbers, for example 15 20.")
        nums.append(int(t))
    first = last = None
    if len(nums) == 1:
        first = last = nums[0]
    elif len(nums) == 2:
        first, last = min(nums), max(nums)
    return toks[0], toks[1], first, last


def plan_text(p: RepostPlan, src: ChannelRef, dst: ChannelRef, already: int, delay: float) -> str:
    kinds = f"{p.text} text, {p.media} media"
    if p.polls:
        kinds += f", {p.polls} poll(s)"
    if p.other:
        kinds += f", {p.other} other"
    lines = [
        "📦 <b>Shift plan</b>",
        f"From <b>{esc(src.title)}</b> → <b>{esc(dst.title)}</b>",
        f"Post ids {p.first}-{p.last}: <b>{p.units} posts</b> to copy ({p.messages} messages, {p.albums} album(s)) - {kinds}",
    ]
    if p.service:
        lines.append(f"• {p.service} service message(s) (like “pinned a message”) can't be copied and are skipped")
    if p.polls:
        lines.append("• polls are copied without their votes")
    if p.dropped_posts:
        lines.append(f"• {p.dropped_posts} post(s) carry other bots' buttons (reactions...) - those are not copied")
    if p.post_links:
        lines.append(
            f"• {p.post_links} link(s) in {p.post_link_posts} post(s) point at other posts of {esc(src.title)}: those that "
            "are shifted too will point at their copies"
        )
    if p.replies:
        lines.append(f"• {p.replies} post(s) answer another post: the copy answers the copy (if that post is shifted too)")
    if already:
        lines.append(
            f"• ⚠️ {already} of these posts were already shifted into {esc(dst.title)} before: they would be copied again"
        )
    minutes = max(1, round(p.eta_seconds(delay) / 60))
    lines += [
        "",
        "<b>How:</b> posts are copied by Telegram itself where possible (exactly as they are); posts with buttons or "
        "replies are posted again with their text, formatting, media and buttons in one go, and every button is read "
        "back to make sure it is there. Copies go to the end of the destination silently, in the original order, and "
        "are registered in My posts.",
        "",
        "<b>Not kept:</b> original dates, view counts, reactions, comments, poll votes and pins.",
        f"Time: about {minutes} min. <b>Nothing is changed in {esc(src.title)}.</b>",
    ]
    return "\n".join(lines)


def register(ctx: Ctx) -> None:
    client, db, cfg = ctx.client, ctx.db, ctx.cfg

    # ---------------------------------------------------------------- views
    async def view_for(sh):
        """Text and buttons describing where a shift stands."""
        dst = await db.get_channel(sh.dst_channel_id)
        c = await db.shift_counts(sh.id)
        src_name = esc(sh.src_title or str(sh.src_channel_id))
        dst_name = esc(dst.title) if dst else str(sh.dst_channel_id)
        route = f"<b>{src_name}</b> → <b>{dst_name}</b>"
        first_copy = ""
        if dst and c["first_new"]:
            first_copy = f'\n<a href="{post_link(dst, c["first_new"])}">First copy</a>'
        undo = [Button.inline("↩️ Undo - remove the copies", f"sfu:{sh.id}")]
        if sh.status == "copied":
            text = (
                f"✅ {route}: <b>{c['copied']} message(s) copied</b> (source ids {sh.first_id}-{sh.last_id}) and "
                f"registered in My posts.{first_copy}\nThe source was not changed. Look at the end of the destination; "
                "if something is wrong, undo removes just these copies."
            )
            kb = [undo]
        elif sh.status == "copies_deleted":
            text = f"↩️ {route}: the copies were removed again."
            kb = []
        elif sh.status == "stopped":
            text = f"⏹ {route}: <b>stopped</b>. {c['copied']} message(s) copied so far.{first_copy}"
            kb = [[Button.inline("▶️ Continue", f"sfc:{sh.id}")], undo]
        elif sh.status == "copying":
            text = f"⏳ {route}: a shift was running when the bot stopped. {c['copied']} message(s) copied.{first_copy}"
            kb = [[Button.inline("▶️ Continue", f"sfc:{sh.id}")], undo]
        else:  # incomplete
            text = (
                f"⚠️ {route}: <b>not everything was copied</b>. {c['copied']} message(s) copied.{first_copy}\n"
                "Nothing was changed in the source."
            )
            kb = [[Button.inline("🔁 Try the rest again", f"sfc:{sh.id}")], undo]
        return text, kb

    # --------------------------------------------------------------- /shift
    @client.on(cmd("shift", args=True))
    @guard(ctx, owner=True)
    async def h_shift(event):
        purge_pending(ctx)
        raw = (event.pattern_match.group(1) or "").strip()
        if not raw:
            sh = await db.open_shift()
            if sh is not None:
                text, kb = await view_for(sh)
                await say(event, "Unfinished shift:\n\n" + text, kb)
            else:
                await say(event, USAGE)
            return
        try:
            src_text, dst_text, first, last = parse_shift_args(raw)
        except ValueError as e:
            raise UserError(f"{e}\n\n" + re.sub(r"<[^>]+>", "", USAGE).replace("&lt;", "<").replace("&gt;", ">"))
        src = await resolve_channel(ctx, src_text)
        dst = await resolve_channel(ctx, dst_text)
        if src.id == dst.id:
            raise UserError("The source and the destination are the same channel. To rebuild one channel in order, use /repost.")
        sh = await db.open_shift(src.id, dst.id)
        if sh is not None:
            text, kb = await view_for(sh)
            await say(event, "There is an unfinished shift between these two channels. Finish or undo it first.\n\n" + text, kb)
            return
        rights = await get_rights(client, dst)
        if rights is not None and not (rights.admin and rights.post):
            raise UserError(f"The bot needs the “Post messages” admin right in {dst.title}.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace / repost / shift) is still running.")
        note = await say(event, f"🔍 Reading <b>{esc(src.title)}</b>…")
        last_edit = [0.0]

        async def prog(p):
            if time.monotonic() - last_edit[0] < 3:
                return
            last_edit[0] = time.monotonic()
            try:
                await note.edit(f"🔍 Reading <b>{esc(src.title)}</b>: post id {p.scanned}…")
            except Exception:
                pass

        try:
            async with ctx.lock:
                plan = await plan_repost(client, src, RepostOptions(), progress=prog, first_id=first, last_id=last)
        except errors.RPCError as e:
            raise UserError(
                f"I can't read {src.title}: {explain_rpc(e)}\nThe bot has to be a member of the source channel "
                "(an admin is best; it only reads)."
            )
        if plan.error:
            await note.edit(f"⚠️ {esc(plan.error)}")
            return
        already = len(await db.shifted_ids(src.id, dst.id, plan.first, plan.last))
        token = secrets.token_hex(4)
        ctx.pending[token] = ShiftJob(user=event.sender_id, src=src, dst=dst, plan=plan, already=already)
        kb = [[Button.inline("▶️ Start copying", f"sfa:{token}"), Button.inline("✖️ Cancel", f"sfn:{token}")]]
        await note.edit(plan_text(plan, src, dst, already, cfg.edit_delay), buttons=kb)

    @on_cb(ctx, "sfn")
    async def cb_cancel(event, parts):
        require_owner(ctx, event)
        ctx.pending.pop(parts[0], None)
        await edit_callback_message(event, "Cancelled - nothing was copied.")

    # ------------------------------------------------------------------ copy
    async def run_copy(event, sid: str, job: Optional[ShiftJob] = None) -> None:
        sh = await db.get_shift(sid)
        dst = await db.get_channel(sh.dst_channel_id)
        if dst is None:
            raise UserError("The destination channel is not registered any more.")
        job = job or ShiftJob(user=event.sender_id, src=None, dst=None, plan=None)  # type: ignore[arg-type]
        job.running, job.stop, job.shift_id = True, False, sid
        ctx.pending[f"job:{sid}"] = job
        total = job.plan.units if job.plan else None
        last = [0.0]
        names = f"<b>{esc(sh.src_title)}</b> → <b>{esc(dst.title)}</b>"

        async def prog(res):
            if res.phase == "copy" and time.monotonic() - last[0] < 4:
                return
            last[0] = time.monotonic()
            done = res.copied_units + res.skipped_done
            of = f"/{total}" if total else ""
            if res.phase == "finish":
                body = f"🔗 Copied {done}{of} posts. Now pointing links at the new copies…"
            else:
                body = f"⏳ Shifting {names}: {done}{of} posts…\nThe source stays untouched."
            try:
                await edit_callback_message(event, body, [[Button.inline("⏹ Stop", f"sfs:{sid}")]])
            except errors.MessageNotModifiedError:
                pass
            except Exception:
                log.debug("progress update failed", exc_info=True)

        try:
            async with ctx.lock:
                await db.set_shift_status(sid, "copying")
                res = await run_shift(
                    client, db, sh, dst, event.sender_id, delay=cfg.edit_delay, progress=prog, should_stop=lambda: job.stop
                )
        finally:
            job.running = False
        status = "stopped" if res.stopped else ("incomplete" if (res.failed or res.aborted) else "copied")
        await db.set_shift_status(sid, status)
        sh = await db.get_shift(sid)
        text, kb = await view_for(sh)
        notes = []
        if res.copied_units:
            notes.append(f"{res.forwarded} post(s) were copied by Telegram, {res.rebuilt} were posted again with their buttons.")
        if res.repaired:
            notes.append(f"{res.repaired} post(s) needed their buttons set again after posting - they show them now.")
        if res.failed:
            sample = ", ".join(f"#{i} {esc(n)}" for i, n in res.failed[:6])
            notes.append(f"<b>{len(res.failed)} post(s) failed</b> ({sample}{'…' if len(res.failed) > 6 else ''}).")
            if any(n == "ButtonsNotShown" for _, n in res.failed):
                notes.append(
                    "ButtonsNotShown: Telegram did not show the buttons on the new post, so that copy was removed again. "
                    "The original is untouched - use “Try the rest again”."
                )
            if any(n == "ReplyParentMissing" for _, n in res.failed):
                notes.append("ReplyParentMissing: that post answers a post whose copy failed; it follows when you try again.")
        if res.aborted:
            notes.append(f"Stopped early because of {esc(res.aborted)}.")
        notes += finishing_notes(res)
        await edit_callback_message(event, text + ("\n\n" + "\n".join(notes) if notes else ""), kb)

    @on_cb(ctx, "sfa")
    async def cb_start(event, parts):
        require_owner(ctx, event)
        job = ctx.pending.get(parts[0])
        if not isinstance(job, ShiftJob) or job.user != event.sender_id:
            raise UserError("That plan has expired. Run /shift again.")
        if job.running or ctx.lock.locked():
            raise UserError("A long job is already running.")
        if await db.open_shift(job.src.id, job.dst.id) is not None:
            raise UserError("There is already an unfinished shift between these two channels.")
        rights = await get_rights(client, job.dst)
        if rights is not None and not (rights.admin and rights.post):
            raise UserError(f"The bot needs the “Post messages” admin right in {job.dst.title}.")
        # the copies are registered in My posts, so the destination has to be one of the bot's channels
        await db.save_channel(job.dst.id, job.dst.access_hash, job.dst.title, job.dst.username, event.sender_id)
        sid = secrets.token_hex(6)
        p = job.plan
        await db.create_shift(sid, src=job.src, dst_channel_id=job.dst.id, first_id=p.first, last_id=p.last, user_id=event.sender_id)
        ctx.pending.pop(parts[0], None)
        await run_copy(event, sid, job)

    @on_cb(ctx, "sfc")
    async def cb_continue(event, parts):
        require_owner(ctx, event)
        sh = await db.get_shift(parts[0])
        if sh is None or sh.status not in ("stopped", "incomplete", "copying"):
            raise UserError("There is nothing to continue.")
        if ctx.lock.locked():
            raise UserError("A long job is already running.")
        await run_copy(event, sh.id)

    @on_cb(ctx, "sfs")
    async def cb_stop(event, parts):
        require_owner(ctx, event)
        job = ctx.pending.get(f"job:{parts[0]}")
        if isinstance(job, ShiftJob) and job.running:
            job.stop = True
            await event.respond("⏹ Stopping after the post that is being copied…")
        else:
            await event.respond("Nothing is running.")

    # ------------------------------------------------------------------ undo
    async def load(parts):
        sh = await db.get_shift(parts[0])
        if sh is None:
            raise UserError("I don't know that shift any more.")
        dst = await db.get_channel(sh.dst_channel_id)
        if dst is None:
            raise UserError("The destination channel is not registered any more.")
        return sh, dst

    @on_cb(ctx, "sfb")
    async def cb_back(event, parts):
        sh, _ = await load(parts)
        text, kb = await view_for(sh)
        await edit_callback_message(event, text, kb)

    @on_cb(ctx, "sfu")
    async def cb_undo_ask(event, parts):
        require_owner(ctx, event)
        sh, dst = await load(parts)
        c = await db.shift_counts(sh.id)
        if not c["new_left"]:
            raise UserError("There is nothing to remove.")
        await edit_callback_message(
            event,
            f"↩️ <b>Remove the {c['new_left']} copied message(s) from {esc(dst.title)}?</b>\n"
            f"The posts in {esc(sh.src_title)} are not touched.",
            [[Button.inline("↩️ Yes, remove the copies", f"sfuy:{sh.id}"), Button.inline("Back", f"sfb:{sh.id}")]],
        )

    @on_cb(ctx, "sfuy")
    async def cb_undo(event, parts):
        require_owner(ctx, event)
        sh, dst = await load(parts)
        if ctx.lock.locked():
            raise UserError("A long job is already running.")
        fallback, deleter = await ctx.userbot.fallback_for(dst) if ctx.userbot is not None else (None, None)
        try:
            async with ctx.lock:
                res = await delete_shift_copies(client, db, dst, sh, delay=0.5, fallback=fallback)
        finally:
            if deleter is not None:
                await deleter.close()
        if res.remaining == 0 and not res.error:
            await db.set_shift_status(sh.id, "copies_deleted")
            await edit_callback_message(
                event, f"↩️ Done. {res.deleted} copied message(s) removed from {esc(dst.title)}; the source was not touched."
            )
        else:
            why = esc(delete_problem_text(ctx.userbot, bot_error=res.error, fallback_error=res.fallback_error, tried=res.tried_userbot))
            await edit_callback_message(
                event,
                f"⚠️ Removed {res.deleted}, {res.remaining} copy/copies are still there. {why}",
                [[Button.inline("🔁 Try again", f"sfuy:{sh.id}")]],
            )

