"""/repost: copy a whole channel in order (links swapped on the way), then remove the old posts. Owner only."""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from telethon import Button, errors

from .. import picker
from ..common import Ctx, UserError, cmd, edit_callback_message, guard, on_cb, purge_pending, require_owner, say, show
from ..crosslinks import CrossResult, progress_text, relink_after_repost, report_lines, restore_after_undo
from ..linkswap import normalize_username
from ..profile_copy import profile_lines
from ..repost_engine import RepostOptions, RepostPlan, delete_copies, plan_repost, run_repost, saved_lookup
from ..tgutil import esc, get_rights, post_link
from ..userbot import delete_problem_text

log = logging.getLogger(__name__)

USAGE = (
    "Usage: <code>/repost @old @new</code>\n"
    "Copies every post of a channel, in order, to the end of the channel and swaps the username inside "
    "t.me links on the way. Leave out the usernames to copy without changing links. "
    "You pick the channel with buttons afterwards.\n"
    "Options: <code>--last 5</code> (trial: only the newest 5 post ids), "
    "<code>--posts</code> (also change t.me/old/123 post links), <code>--no-typed</code>.\n\n"
    "Links to the old posts - in this channel and in all your other connected channels - are pointed at the copies "
    "afterwards.\n"
    "Nothing is deleted by /repost itself: you decide about the old posts after checking the copies."
)


@dataclass
class RepostChoice:
    """/repost was typed; the owner still has to pick the channel."""

    user: int
    opts: RepostOptions
    created: float = field(default_factory=time.monotonic)


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
    flags = {"last": None, "posts": False, "typed": None}
    pos: list = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--channel":
            raise ValueError("/repost no longer takes --channel: send the command without it and pick the channel with the buttons.")
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
    lines += held_plan_lines(p, title)
    if p.post_links:
        lines.append(
            f"• {p.post_links} link(s) in {p.post_link_posts} post(s) point at other posts of this channel: "
            "they are pointed at the new copies (a link to a post that doesn't exist stays as it is)"
        )
    lines.append(
        "• afterwards every link to an old post - a hyperlink, a typed link or a button, in ANY of your connected "
        "channels - is pointed at the copy (the old posts themselves are not touched)"
    )
    if p.replies:
        lines.append(f"• {p.replies} post(s) answer another post: each copy answers the copy of that post")
    if p.pinned:
        lines.append(
            f"• {p.pinned} pinned post(s): their copies are pinned at the end "
            "(the bot needs the “Edit messages of others” right for that)"
        )
    minutes = max(1, round(p.eta_seconds(delay) / 60))
    lines += [
        "",
        "<b>How:</b> posts without buttons, replies or links to change are copied by Telegram itself, so they stay "
        "exactly as they are. Posts with buttons, links to change or a reply are posted again with their text, "
        "formatting, media and buttons in one go, and each one is read back to make sure its buttons are really "
        "there. Copies go to the end of the channel silently, in the original order, and are registered in My posts.",
        "",
        "<b>Not kept:</b> original dates, view counts, reactions, comments and poll votes.",
        f"Time: about {minutes} min. Please don't post in the channel meanwhile.",
        "<b>Nothing is deleted in this step.</b>",
    ]
    if p.partial:
        lines.append("Trial run: afterwards you can remove the copies again.")
    return "\n".join(lines)


def held_plan_lines(p: RepostPlan, title: str, *, shift: bool = False) -> list:
    """Plan lines about posts Telegram holds back (it shows a notice, for example for a copyright strike, instead of them)."""
    lines = []
    name = esc(title)
    if p.channel_restricted:
        lines.append(f"• ⚠️ Telegram restricts {name} itself: “{esc(p.channel_restricted)}”")
    said = f" (“{esc(p.held_why)}”)" if p.held_why else ""
    if p.held_saved:
        lines.append(
            f"• 📋 Telegram holds back {p.held_saved} post(s) of {name}{said} - it shows a notice instead of them. "
            "Those are copied from what My posts saved of them: text, formatting, buttons and media (a file that "
            "Telegram refuses goes out without it)."
        )
    if p.held_skipped and shift:
        lines.append(
            f"• ⚠️ Telegram holds back {p.held_skipped} more post(s) of {name}{said} and My posts has no saved copy of "
            "them, so they are skipped and the shift will show as not complete. If you have a backup made before that, "
            "put it back with /import and run /shift again."
        )
    elif p.held_skipped:
        lines.append(
            f"• ⚠️ Telegram holds back {p.held_skipped} post(s) of {name}{said} - it shows a notice instead of them. "
            "Copying the notice would be wrong, so they are skipped, and the old posts can't be deleted afterwards "
            "(the repost will show as not complete)."
        )
    return lines


def held_notes(res) -> list:
    """Lines about the posts Telegram holds back, for the end of a repost or shift."""
    notes = []

    def sample(ids, n=8):
        return ", ".join(f"#{i}" for i in ids[:n]) + ("…" if len(ids) > n else "")

    if res.from_db:
        if res.from_saved:
            notes.append(
                f"📋 The source can't be read, so {res.from_saved} post(s) were copied from what My posts saved of them: "
                "text, formatting, buttons and media - without replies, pins and albums."
            )
    elif res.from_saved:
        notes.append(
            f"📋 {res.from_saved} post(s) are held back by Telegram - it shows a notice instead of them. They were "
            "copied from what My posts saved of them: text, formatting, buttons and media."
        )
    if res.media_lost:
        notes.append(
            f"⚠️ {len(res.media_lost)} of them went out without their media, because Telegram refuses the saved file "
            f"({sample(res.media_lost)})."
        )
    if res.held_back and res.used_saved:
        notes.append(
            f"⚠️ {len(res.held_back)} post(s) are held back by Telegram and My posts has no saved copy of them, so they "
            f"were not copied ({sample(res.held_back)}). Put a backup made before that back with /import, then use "
            "“Try the rest again”."
        )
    elif res.held_back:
        notes.append(
            f"⚠️ {len(res.held_back)} post(s) are held back by Telegram - it shows a notice instead of them "
            f"({sample(res.held_back)}). Copying the notice would be wrong, so they were not copied and the old posts are "
            "not deleted."
        )
    return notes


def finishing_notes(res) -> list:
    """Lines about answers, links and pins for the end of a repost."""
    notes = held_notes(res)
    if res.replies:
        notes.append(f"↩️ {res.replies} post(s) answer the copy of the post they answered.")
    if res.reply_lost:
        notes.append(f"{res.reply_lost} post(s) could only be copied without their reply (their media can't be rebuilt).")
    if res.reply_dropped:
        notes.append(f"{res.reply_dropped} post(s) answered a post that is not there any more, so they answer nothing.")
    fin = res.final
    if fin is not None:
        if fin.relinked:
            notes.append(f"🔗 {fin.links} link(s) in {fin.relinked} post(s) now point at the new copies.")
        if fin.failed:
            sample = ", ".join(f"#{i} {esc(n)}" for i, n in fin.failed[:5])
            notes.append(f"{len(fin.failed)} post(s) could not get their links updated ({sample}).")
        if fin.pinned:
            notes.append(f"📌 {fin.pinned} pinned post(s) are pinned again.")
        if fin.pin_error:
            notes.append(
                f"📌 {fin.pin_wanted - fin.pinned} copy/copies could not be pinned ({esc(fin.pin_error)}). Give the bot the "
                "“Edit messages of others” right, or pin them by hand."
            )
    if res.final_error:
        notes.append(f"Links and pins could not be finished ({esc(res.final_error)}).")
    if res.profile is not None:
        notes += profile_lines(res.profile)
    if res.profile_error:
        notes.append(f"The name, description and photo could not be copied ({esc(res.profile_error)}).")
    if res.cross is not None:
        notes += report_lines(res.cross)
    if res.cross_error:
        notes.append(
            f"Links in your other channels could not be updated ({esc(res.cross_error)}). "
            "Press “🔗 Update links again” to try once more."
        )
    return notes


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
        links = [Button.inline("🔗 Update links again", f"rpl:{mig.id}")]
        if mig.status == "copied" and not mig.partial:
            text = (
                f"✅ <b>{name}: all posts copied in order</b>{swap}\n{c['copied']} messages are now at the end of the channel "
                f"and in My posts.{first_copy}\n\nLook at the end of the channel and check a few posts: formatting, "
                "buttons, media. If it looks right, delete the old posts. If not, undo: the copies are removed and the "
                "originals were never touched."
            )
            kb = [[Button.inline("🗑 Delete the old posts", f"rpd:{mig.id}")], links, undo]
        elif mig.status == "copied":
            text = (
                f"✅ <b>{name}: trial finished</b>{swap}\n{c['copied']} message(s) copied.{first_copy}\n"
                "Check them. Remove them again, or close this trial and keep them."
            )
            kb = [links, undo, [Button.inline("✅ Close, keep the copies", f"rpk:{mig.id}")]]
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
        purge_pending(ctx)
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
        except ValueError as e:
            raise UserError(f"{e}\n\n" + re.sub(r"<[^>]+>", "", USAGE))
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels registered yet. Use /addchannel.")
        opts = RepostOptions(
            old=old,
            new=new,
            include_typed=cfg.replace_typed_links if flags["typed"] is None else flags["typed"],
            include_posts=flags["posts"],
            last=flags["last"],
        )
        token = secrets.token_hex(4)
        ctx.pending[token] = RepostChoice(user=event.sender_id, opts=opts)
        await show_picker(event, token, 0)

    async def show_picker(event, token: str, page: int) -> None:
        require_owner(ctx, event)
        choice = ctx.pending.get(token)
        if not isinstance(choice, RepostChoice) or choice.user != event.sender_id:
            raise UserError("That list has expired. Run /repost again.")
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels registered yet. Use /addchannel.")
        opts = choice.opts
        what = (
            f"swap <code>@{esc(opts.old)}</code> → <code>@{esc(opts.new)}</code> in the links"
            if opts.swaps
            else "copy without changing any link"
        )
        if opts.last:
            what += f", trial: only the newest {opts.last} post ids"
        text, kb = picker.render(
            chans,
            page,
            head=f"📢 <b>Which channel should I repost?</b>\nPlan: {what}.",
            kind="rp",
            arg=token,
            choose=lambda c: f"rpch:{token}:{c.id}",
            extra_rows=[[Button.inline("✖️ Cancel", f"rpn:{token}")]],
        )
        await show(event, text, kb)

    ctx.pickers["rp"] = show_picker

    @on_cb(ctx, "rpch")
    async def cb_pick(event, parts):
        require_owner(ctx, event)
        token, cid = parts[0], int(parts[1])
        choice = ctx.pending.get(token)
        if not isinstance(choice, RepostChoice) or choice.user != event.sender_id:
            raise UserError("That list has expired. Run /repost again.")
        ch = await db.get_channel(cid)
        if ch is None or not ch.active:
            raise UserError("That channel is not registered any more. Run /repost again.")
        mig = await db.open_migration(ch.id)
        if mig is not None:
            ctx.pending.pop(token, None)
            text, kb = await view_for(mig)
            await edit_callback_message(
                event, "There is an unfinished repost for this channel. Finish or undo it first.\n\n" + text, kb
            )
            return
        rights = await get_rights(client, ch)
        if rights is not None and not (rights.admin and rights.post):
            raise UserError("The bot needs the “Post messages” admin right in that channel.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace / repost) is still running.")
        ctx.pending.pop(token, None)  # a second press on the list can't start a second plan
        opts = choice.opts
        last = [0.0]

        async def prog(p):
            if time.monotonic() - last[0] < 3:
                return
            last[0] = time.monotonic()
            try:
                await edit_callback_message(event, f"🔍 Reading <b>{esc(ch.title)}</b>: post id {p.scanned}…")
            except Exception:
                pass

        async with ctx.lock:
            await edit_callback_message(event, f"🔍 Reading <b>{esc(ch.title)}</b>…")
            plan = await plan_repost(client, ch, opts, progress=prog, saved=saved_lookup(db, ch.id))
        if plan.error:
            await edit_callback_message(event, f"⚠️ {esc(plan.error)}")
            return
        job = RepostJob(user=event.sender_id, ch=ch, opts=opts, plan=plan)
        ctx.pending[token] = job
        kb = [[Button.inline("▶️ Start copying", f"rpa:{token}"), Button.inline("✖️ Cancel", f"rpn:{token}")]]
        await edit_callback_message(event, plan_text(plan, opts, ch.title, cfg.edit_delay), kb)

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
            if res.phase in ("copy", "links") and time.monotonic() - last[0] < 4:
                return
            last[0] = time.monotonic()
            done = res.copied_units + res.skipped_done
            of = f"/{total}" if total else ""
            if res.phase == "links" and res.cross is not None:
                body = f"✅ Copied {done}{of} posts.\n" + progress_text(res.cross)
            elif res.phase == "finish":
                body = f"🔗 Copied {done}{of} posts. Now pointing links at the new copies and pinning…"
            else:
                body = f"⏳ Copying <b>{esc(ch.title)}</b>: {done}{of} posts…\nThe old posts stay untouched."
            try:
                await edit_callback_message(event, body, [[Button.inline("⏹ Stop", f"rps:{mig.id}")]])
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
        status = "stopped" if res.stopped else ("incomplete" if (res.failed or res.aborted or res.held_back) else "copied")
        await db.set_migration_status(mig.id, status)
        mig = await db.get_migration(mig.id)
        text, kb = await view_for(mig)
        notes = []
        if res.copied_units:
            notes.append(
                f"{res.forwarded} post(s) were copied by Telegram, {res.rebuilt} were posted again with their buttons."
            )
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
        if res.aborted:
            notes.append(f"Stopped early because of {esc(res.aborted)}.")
        notes += finishing_notes(res)
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
            await event.respond("⏹ Stopping after the post that is being worked on…")
        else:
            await event.respond("Nothing is running.")

    # ------------------------------------------------------------ links again
    @on_cb(ctx, "rpl")
    async def cb_links(event, parts):
        """Look through every connected channel once more for links to the old posts and point them at the copies."""
        require_owner(ctx, event)
        mig, ch = await load(parts)
        if mig.status not in ("copied", "old_deleted"):
            raise UserError("Links can be updated once everything is copied.")
        if ctx.lock.locked():
            raise UserError("A long job is already running.")
        job = RepostJob(user=event.sender_id, ch=ch, opts=RepostOptions(), mig_id=mig.id, running=True)
        ctx.pending[f"job:{mig.id}"] = job
        cross, last = CrossResult(), [0.0]

        async def prog(c):
            if time.monotonic() - last[0] < 4:
                return
            last[0] = time.monotonic()
            try:
                await edit_callback_message(event, progress_text(c), [[Button.inline("⏹ Stop", f"rps:{mig.id}")]])
            except errors.MessageNotModifiedError:
                pass
            except Exception:
                log.debug("progress update failed", exc_info=True)

        error = None
        try:
            async with ctx.lock:
                await edit_callback_message(event, "🔗 Looking for links to the old posts in your channels…")
                await relink_after_repost(
                    client, db, ch, mig, delay=cfg.edit_delay, progress=prog, should_stop=lambda: job.stop, result=cross
                )
        except Exception as e:  # the posts are fine; only the links were not (all) updated
            log.exception("pointing the links at the copies failed")
            error = type(e).__name__
        finally:
            job.running = False
            ctx.pending.pop(f"job:{mig.id}", None)
        notes = report_lines(cross)
        if error:
            notes.append(f"Links could not be updated ({esc(error)}).")
        if not notes:
            notes.append("🔗 There is nothing to change.")
        again = [Button.inline("🔗 Update links again", f"rpl:{mig.id}")]
        if mig.status == "copied":
            text, kb = await view_for(mig)
            await edit_callback_message(event, text + "\n\n" + "\n".join(notes), kb)
        else:
            await edit_callback_message(event, f"🔗 <b>{esc(ch.title)}</b>\n" + "\n".join(notes), [again])

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
            "If Telegram refuses (rights, or an age limit), the userbot - if you set one up - deletes what the bot can't. "
            "Nothing is lost either way: the copies are already there.",
            [[Button.inline(f"🗑 Yes, delete {c['old_left']}", f"rpdy:{mig.id}"), Button.inline("↩️ Back", f"rpb:{mig.id}")]],
        )

    def userbot_lines(deleter) -> str:
        notes = getattr(deleter, "notes", None) or []
        return ("\n" + "\n".join(f"• {esc(n)}" for n in notes)) if notes else ""

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

        fallback, deleter = await ctx.userbot.fallback_for(ch) if ctx.userbot is not None else (None, None)
        try:
            async with ctx.lock:
                res = await delete_copies(client, db, ch, mig, "old", delay=0.5, progress=prog, fallback=fallback)
        finally:
            if deleter is not None:
                await deleter.close()
        c = await db.migration_counts(mig.id)
        extra = userbot_lines(deleter)
        by = f" ({res.by_userbot} of them by the userbot)" if res.by_userbot else ""
        if res.remaining == 0 and not res.error:
            await db.set_migration_status(mig.id, "old_deleted")
            await edit_callback_message(
                event,
                f"✅ <b>Done.</b> {res.deleted} old message(s) deleted{by}. The channel now consists of the copies "
                f"(from <a href=\"{post_link(ch, c['first_new'])}\">here</a>), and they are all in My posts.{extra}",
                [[Button.inline("🔗 Update links again", f"rpl:{mig.id}")]],
            )
            return
        why = esc(
            delete_problem_text(
                ctx.userbot, bot_error=res.error, fallback_error=res.fallback_error, tried=res.tried_userbot
            )
        )
        await edit_callback_message(
            event,
            f"⚠️ Deleted {res.deleted}{by}, {res.remaining} old message(s) are still there. {why}{extra}\n"
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
        fallback, deleter = await ctx.userbot.fallback_for(ch) if ctx.userbot is not None else (None, None)
        back = CrossResult()
        last = [0.0]

        async def prog(c):
            if time.monotonic() - last[0] < 4:
                return
            last[0] = time.monotonic()
            try:
                await edit_callback_message(event, progress_text(c, title="Pointing links back at the original posts"))
            except Exception:
                log.debug("progress update failed", exc_info=True)

        try:
            async with ctx.lock:
                try:  # links that were pointed at the copies would be left without a post: point them back first
                    await restore_after_undo(client, db, ch, mig, delay=cfg.edit_delay, progress=prog, result=back)
                except Exception:
                    log.exception("pointing the links back at the original posts failed")
                    back.stopped = True
                res = await delete_copies(client, db, ch, mig, "new", delay=0.5, fallback=fallback)
        finally:
            if deleter is not None:
                await deleter.close()
        link_notes = report_lines(back, back=True)
        link_text = ("\n" + "\n".join(link_notes)) if link_notes else ""
        if res.remaining == 0 and not res.error:
            await db.set_migration_status(mig.id, "copies_deleted")
            await edit_callback_message(
                event,
                f"↩️ Done. {res.deleted} copied message(s) removed; the original posts were not touched."
                f"{userbot_lines(deleter)}{link_text}",
            )
        else:
            why = esc(
                delete_problem_text(
                    ctx.userbot, bot_error=res.error, fallback_error=res.fallback_error, tried=res.tried_userbot
                )
            )
            await edit_callback_message(
                event,
                f"⚠️ Removed {res.deleted}, {res.remaining} copy/copies are still there. {why}{link_text}",
                [[Button.inline("🔁 Try again", f"rpuy:{mig.id}")]],
            )

    @on_cb(ctx, "rpk")
    async def cb_close(event, parts):
        require_owner(ctx, event)
        mig, _ = await load(parts)
        await db.set_migration_status(mig.id, "closed")
        await edit_callback_message(event, "✅ Closed. Nothing else will be changed in that channel by this repost.")
