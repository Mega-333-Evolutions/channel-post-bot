"""/repost: copy a whole channel in order (links swapped on the way), then remove the old posts. Owner only."""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from telethon import Button, errors

from ..common import Ctx, UserError, cmd, edit_callback_message, guard, on_cb, require_owner, say
from ..linkswap import normalize_username
from ..repost_engine import RepostOptions, RepostPlan, delete_copies, plan_repost, run_repost
from ..tgutil import esc, get_rights, post_link
from .replace import pick_channels

log = logging.getLogger(__name__)

USAGE = (
    "Usage: <code>/repost @old @new</code>\n"
    "Copies every post of the channel, in order, to the end of the channel and swaps the username inside "
    "t.me links on the way. Leave out the usernames to copy without changing links.\n"
    "Options: <code>--channel @name</code>, <code>--last 5</code> (trial: only the newest 5 post ids), "
    "<code>--posts</code> (also change t.me/old/123 post links), <code>--no-typed</code>.\n\n"
    "Nothing is deleted by /repost itself: you decide about the old posts after checking the copies."
)


@dataclass
class RepostJob:
    user: int
    ch: object
    opts: RepostOptions
    plan: object = None
    running: bool = False
    stop: bool = False
    mig_id: str = ""
    created: float = field(default_factory=time.monotonic)


def parse_repost_args(raw: str) -> tuple:
    toks = (raw or "").split()
    flags = {"channel": None, "last": None, "posts": False, "typed": None}
    pos: list = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--channel" and i + 1 < len(toks):
            flags["channel"] = toks[i + 1]
            i += 2
        elif t == "--last" and i + 1 < len(toks):
            if not toks[i + 1].isdigit() or int(toks[i + 1]) < 1:
                raise ValueError("--last needs a number, for example --last 5")
            flags["last"] = int(toks[i + 1])
            i += 2
        elif t == "--posts":
            flags["posts"] = True
            i += 1
        elif t == "--no-typed":
            flags["typed"] = False
            i += 1
        elif t.startswith("--"):
            raise ValueError(f"Unknown option {t}")
        else:
            pos.append(t)
            i += 1
    old = new = None
    if pos:
        if len(pos) != 2:
            raise ValueError("Give both usernames (@old @new), or none to copy without changing links.")
        old, new = normalize_username(pos[0]), normalize_username(pos[1])
        if not old or not new:
            raise ValueError("Usernames look like @name (letters, digits and underscores).")
        if old.lower() == new.lower():
            raise ValueError("The old and the new username are the same.")
    return old, new, flags


def plan_text(p: RepostPlan, o: RepostOptions, title: str, delay: float) -> str:
    kinds = f"{p.text} text, {p.media} media"
    if p.polls:
        kinds += f", {p.polls} poll(s)"
    if p.other:
        kinds += f", {p.other} other"
    lines = [
        f"🧱 <b>Repost plan - {esc(title)}</b>",
        f"Post ids {p.first}-{p.last}: <b>{p.units} posts</b> to copy ({p.messages} messages, {p.albums} album(s)) - {kinds}",
    ]
    if p.service:
        lines.append(f"• {p.service} service message(s) (like “pinned a message”) can't be copied and are skipped")
    if p.polls:
        lines.append("• polls are copied without their votes")
    if o.swaps:
        lines.append(
            f"• <code>@{esc(o.old)}</code> → <code>@{esc(o.new)}</code> in {p.link_posts} post(s): "
            f"{p.n_buttons} button link(s), {p.n_links} hyperlink(s), {p.n_typed} typed link(s)"
        )
    if p.dropped_posts:
        lines.append(f"• {p.dropped_posts} post(s) carry other bots' buttons (reactions...) - those are not copied")
    minutes = max(1, round(p.eta_seconds(delay) / 60))
    lines += [
        "",
        "<b>How:</b> Telegram copies each post itself, so bold, links, quotes, expandable quotes, spoilers, media and "
        "albums stay exactly as they are (if the channel blocks copying, posts are rebuilt from their parts). Copies go "
        "to the end of the channel silently, in the original order, and are registered in My posts.",
        "",
        "<b>Not kept:</b> original dates, view counts, reactions, comments, poll votes, pinned state and links to the "
        "old posts.",
        f"Time: about {minutes} min. Please don't post in the channel meanwhile.",
        "<b>Nothing is deleted in this step.</b>",
    ]
    if p.partial:
        lines.append("Trial run: afterwards you can remove the copies again.")
    return "\n".join(lines)


def register(ctx: Ctx) -> None:
    client, db, cfg = ctx.client, ctx.db, ctx.cfg

    # ---------------------------------------------------------------- views
    async def view_for(mig):
        """Text and buttons describing where a repost stands."""
        ch = await db.get_channel(mig.channel_id)
        c = await db.migration_counts(mig.id)
        name = esc(ch.title) if ch else str(mig.channel_id)
        swap = f" (<code>@{esc(mig.old_username)}</code> → <code>@{esc(mig.new_username)}</code>)" if mig.old_username else ""
        first_copy = ""
        if ch and c["first_new"]:
            first_copy = f'\n<a href="{post_link(ch, c["first_new"])}">First copy</a>'
        undo = [Button.inline("↩️ Undo - remove the copies", f"rpu:{mig.id}")]
        if mig.status == "copied" and not mig.partial:
            text = (
                f"✅ <b>{name}: all posts copied in order</b>{swap}\n{c['copied']} messages are now at the end of the channel "
                f"and in My posts.{first_copy}\n\nLook at the end of the channel and check a few posts: formatting, "
                "buttons, media. If it looks right, delete the old posts. If not, undo: the copies are removed and the "
                "originals were never touched."
            )
            kb = [[Button.inline("🗑 Delete the old posts", f"rpd:{mig.id}")], undo]
        elif mig.status == "copied":
            text = (
                f"✅ <b>{name}: trial finished</b>{swap}\n{c['copied']} message(s) copied.{first_copy}\n"
                "Check them. Remove them again, or close this trial and keep them."
            )
            kb = [undo, [Button.inline("✅ Close, keep the copies", f"rpk:{mig.id}")]]
        elif mig.status == "stopped":
            text = f"⏹ <b>{name}: stopped</b>{swap}\n{c['copied']} message(s) copied so far.{first_copy}"
            kb = [[Button.inline("▶️ Continue", f"rpc:{mig.id}")], undo]
        elif mig.status == "copying":
            text = f"⏳ <b>{name}</b>: a repost was running when the bot stopped. {c['copied']} message(s) copied.{first_copy}"
            kb = [[Button.inline("▶️ Continue", f"rpc:{mig.id}")], undo]
        else:  # incomplete
            text = (
                f"⚠️ <b>{name}: not everything was copied</b>{swap}\n{c['copied']} message(s) copied.{first_copy}\n"
                "Old posts are only deleted once everything is copied, so nothing is lost. Try the rest again, "
                "or undo."
            )
            kb = [[Button.inline("🔁 Try the rest again", f"rpc:{mig.id}")], undo]
        return text, kb

    # --------------------------------------------------------------- /repost
    @client.on(cmd("repost", args=True))
    @guard(ctx, owner=True)
    async def h_repost(event):
        raw = (event.pattern_match.group(1) or "").strip()
        if not raw:
            mig = await db.open_migration()
            if mig is not None:
                text, kb = await view_for(mig)
                await say(event, "Unfinished repost:\n\n" + text, kb)
            else:
                await say(event, USAGE)
            return
        try:
            old, new, flags = parse_repost_args(raw)
            chans = await db.list_channels()
            if flags["channel"]:
                chans = pick_channels(chans, flags["channel"])
            elif len(chans) > 1:
                raise ValueError("You have several channels - add --channel @name.")
        except ValueError as e:
            raise UserError(f"{e}\n\n" + re.sub(r"<[^>]+>", "", USAGE))
        if not chans:
            raise UserError("No channels registered yet. Use /addchannel.")
        ch = chans[0]
        mig = await db.open_migration(ch.id)
        if mig is not None:
            text, kb = await view_for(mig)
            await say(event, "There is an unfinished repost for this channel. Finish or undo it first.\n\n" + text, kb)
            return
        rights = await get_rights(client, ch)
        if rights is not None and not (rights.admin and rights.post):
            raise UserError("The bot needs the “Post messages” admin right in that channel.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace / repost) is still running.")
        opts = RepostOptions(
            old=old,
            new=new,
            include_typed=cfg.replace_typed_links if flags["typed"] is None else flags["typed"],
            include_posts=flags["posts"],
            last=flags["last"],
        )
        async with ctx.lock:
            status = await say(event, f"🔍 Reading <b>{esc(ch.title)}</b>…")
            last = [0.0]

            async def prog(p):
                if time.monotonic() - last[0] < 3:
                    return
                last[0] = time.monotonic()
                try:
                    await status.edit(f"🔍 Reading <b>{esc(ch.title)}</b>: post id {p.scanned}…")
                except Exception:
                    pass

            plan = await plan_repost(client, ch, opts, progress=prog)
        if plan.error:
            await status.edit(f"⚠️ {esc(plan.error)}")
            return
        job = RepostJob(user=event.sender_id, ch=ch, opts=opts, plan=plan)
        token = secrets.token_hex(4)
        ctx.pending[token] = job
        kb = [[Button.inline("▶️ Start copying", f"rpa:{token}"), Button.inline("✖️ Cancel", f"rpn:{token}")]]
        await status.edit(plan_text(plan, opts, ch.title, cfg.edit_delay), buttons=kb, link_preview=False)

    @on_cb(ctx, "rpn")
    async def cb_cancel(event, parts):
        require_owner(ctx, event)
        ctx.pending.pop(parts[0], None)
        await edit_callback_message(event, "Cancelled - nothing was copied or deleted.")

    # ------------------------------------------------------------------ copy
    async def run_copy(event, job: RepostJob) -> None:
        mig = await db.get_migration(job.mig_id)
        ch = job.ch
        job.running, job.stop = True, False
        ctx.pending[f"job:{mig.id}"] = job
        total = job.plan.units if job.plan else None
        last = [0.0]

        async def prog(res):
            if time.monotonic() - last[0] < 4:
                return
            last[0] = time.monotonic()
            done = res.copied_units + res.skipped_done
            of = f"/{total}" if total else ""
            try:
                await edit_callback_message(
                    event,
                    f"⏳ Copying <b>{esc(ch.title)}</b>: {done}{of} posts…\nThe old posts stay untouched.",
                    [[Button.inline("⏹ Stop", f"rps:{mig.id}")]],
                )
            except errors.MessageNotModifiedError:
                pass
            except Exception:
                log.debug("progress update failed", exc_info=True)

        try:
            async with ctx.lock:
                await db.set_migration_status(mig.id, "copying")
                res = await run_repost(
                    client, db, ch, mig, event.sender_id, delay=cfg.edit_delay, progress=prog, should_stop=lambda: job.stop
                )
        finally:
            job.running = False
        status = "stopped" if res.stopped else ("incomplete" if (res.failed or res.aborted) else "copied")
        await db.set_migration_status(mig.id, status)
        mig = await db.get_migration(mig.id)
        text, kb = await view_for(mig)
        notes = []
        if res.rebuilt:
            notes.append(f"{res.rebuilt} post(s) had to be rebuilt from their parts instead of copied by Telegram.")
        if res.failed:
            sample = ", ".join(f"#{i} {esc(n)}" for i, n in res.failed[:6])
            notes.append(f"<b>{len(res.failed)} post(s) failed</b> ({sample}{'…' if len(res.failed) > 6 else ''}).")
        if res.aborted:
            notes.append(f"Stopped early because of {esc(res.aborted)}.")
        await edit_callback_message(event, text + ("\n\n" + "\n".join(notes) if notes else ""), kb)

    @on_cb(ctx, "rpa")
    async def cb_start(event, parts):
        require_owner(ctx, event)
        job = ctx.pending.get(parts[0])
        if not isinstance(job, RepostJob) or job.user != event.sender_id:
            raise UserError("That plan has expired. Run /repost again.")
        if job.running or ctx.lock.locked():
            raise UserError("A long job is already running.")
        if await db.open_migration(job.ch.id) is not None:
            raise UserError("There is already an unfinished repost for this channel.")
        p, o = job.plan, job.opts
        job.mig_id = secrets.token_hex(6)
        await db.create_migration(
            job.mig_id, job.ch.id, old=o.old, new=o.new, include_typed=o.include_typed, include_posts=o.include_posts,
            first_id=p.first, last_id=p.last, partial=p.partial, user_id=event.sender_id,
        )
        ctx.pending.pop(parts[0], None)
        await run_copy(event, job)

    @on_cb(ctx, "rpc")
    async def cb_continue(event, parts):
        require_owner(ctx, event)
        mig = await db.get_migration(parts[0])
        if mig is None or mig.status not in ("stopped", "incomplete", "copying"):
            raise UserError("There is nothing to continue.")
        if ctx.lock.locked():
            raise UserError("A long job is already running.")
        ch = await db.get_channel(mig.channel_id)
        if ch is None:
            raise UserError("That channel is not registered any more.")
        opts = RepostOptions(old=mig.old_username, new=mig.new_username, include_typed=mig.include_typed, include_posts=mig.include_posts)
        await run_copy(event, RepostJob(user=event.sender_id, ch=ch, opts=opts, mig_id=mig.id))

    @on_cb(ctx, "rps")
    async def cb_stop(event, parts):
        require_owner(ctx, event)
        job = ctx.pending.get(f"job:{parts[0]}")
        if isinstance(job, RepostJob) and job.running:
            job.stop = True
            await event.respond("⏹ Stopping after the post that is being copied…")
        else:
            await event.respond("Nothing is running.")

    # ----------------------------------------------------- delete old / undo
    async def load(parts):
        mig = await db.get_migration(parts[0])
        if mig is None:
            raise UserError("I don't know that repost any more.")
        ch = await db.get_channel(mig.channel_id)
        if ch is None:
            raise UserError("That channel is not registered any more.")
        return mig, ch

    @on_cb(ctx, "rpb")
    async def cb_back(event, parts):
        mig, _ = await load(parts)
        text, kb = await view_for(mig)
        await edit_callback_message(event, text, kb)

    @on_cb(ctx, "rpd")
    async def cb_delete_ask(event, parts):
        require_owner(ctx, event)
        mig, ch = await load(parts)
        if mig.status != "copied" or mig.partial:
            raise UserError("Old posts can only be deleted after a complete, successful copy.")
        c = await db.migration_counts(mig.id)
        await edit_callback_message(
            event,
            f"🗑 <b>Delete {c['old_left']} old message(s) from {esc(ch.title)}?</b>\n"
            "Only originals that have a copy are deleted. This can't be undone. The copies stay and become the channel.\n"
            "If Telegram refuses (rights, or an age limit), nothing is lost: the copies are already there.",
            [[Button.inline(f"🗑 Yes, delete {c['old_left']}", f"rpdy:{mig.id}"), Button.inline("↩️ Back", f"rpb:{mig.id}")]],
        )

    @on_cb(ctx, "rpdy")
    async def cb_delete_old(event, parts):
        require_owner(ctx, event)
        mig, ch = await load(parts)
        if mig.status not in ("copied", "old_deleted") or mig.partial:
            raise UserError("Old posts can only be deleted after a complete, successful copy.")
        if ctx.lock.locked():
            raise UserError("A long job is already running.")
        last = [0.0]

        async def prog(r):
            if time.monotonic() - last[0] < 3:
                return
            last[0] = time.monotonic()
            try:
                await edit_callback_message(event, f"🗑 Deleting old posts… {r.deleted} done")
            except Exception:
                pass

        async with ctx.lock:
            res = await delete_copies(client, db, ch, mig, "old", delay=0.5, progress=prog)
        c = await db.migration_counts(mig.id)
        if res.remaining == 0 and not res.error:
            await db.set_migration_status(mig.id, "old_deleted")
            await edit_callback_message(
                event,
                f"✅ <b>Done.</b> {res.deleted} old message(s) deleted. The channel now consists of the copies "
                f"(from <a href=\"{post_link(ch, c['first_new'])}\">here</a>), and they are all in My posts.",
            )
            return
        why = f"Telegram error: {esc(res.error)}." if res.error else (
            "Telegram refused to delete the next batch (the bot's “Delete messages” right, or an age limit on old posts)."
        )
        await edit_callback_message(
            event,
            f"⚠️ Deleted {res.deleted}, {res.remaining} old message(s) are still there. {why}\n"
            f"The copies are complete. You can delete the rest by hand: they are the messages with an id below "
            f"{c['first_new']}. Then close this repost.",
            [[Button.inline("🔁 Try again", f"rpdy:{mig.id}")], [Button.inline("✅ Close this repost", f"rpk:{mig.id}")]],
        )

    @on_cb(ctx, "rpu")
    async def cb_undo_ask(event, parts):
        require_owner(ctx, event)
        mig, ch = await load(parts)
        c = await db.migration_counts(mig.id)
        if mig.status in ("old_deleted", "copies_deleted", "closed") or c["old_left"] < c["copied"]:
            raise UserError("Some originals are already deleted, so removing the copies would delete your content. Undo is not possible any more.")
        await edit_callback_message(
            event,
            f"↩️ <b>Remove the {c['new_left']} copied message(s) from {esc(ch.title)}?</b>\nThe original posts stay as they are.",
            [[Button.inline("↩️ Yes, remove the copies", f"rpuy:{mig.id}"), Button.inline("Back", f"rpb:{mig.id}")]],
        )

    @on_cb(ctx, "rpuy")
    async def cb_undo(event, parts):
        require_owner(ctx, event)
        mig, ch = await load(parts)
        c = await db.migration_counts(mig.id)
        if mig.status in ("old_deleted", "copies_deleted", "closed") or c["old_left"] < c["copied"]:
            raise UserError("Undo is not possible any more.")
        if ctx.lock.locked():
            raise UserError("A long job is already running.")
        async with ctx.lock:
            res = await delete_copies(client, db, ch, mig, "new", delay=0.5)
        if res.remaining == 0 and not res.error:
            await db.set_migration_status(mig.id, "copies_deleted")
            await edit_callback_message(event, f"↩️ Done. {res.deleted} copied message(s) removed; the original posts were not touched.")
        else:
            await edit_callback_message(
                event,
                f"⚠️ Removed {res.deleted}, {res.remaining} copy/copies are still there ({esc(res.error or 'Telegram refused')}).",
                [[Button.inline("🔁 Try again", f"rpuy:{mig.id}")]],
            )

    @on_cb(ctx, "rpk")
    async def cb_close(event, parts):
        require_owner(ctx, event)
        mig, _ = await load(parts)
        await db.set_migration_status(mig.id, "closed")
        await edit_callback_message(event, "✅ Closed. Nothing else will be changed in that channel by this repost.")
